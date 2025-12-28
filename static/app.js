// CEO Dashboard Logic

// State
const state = {
    agents: {},
    inbox: [],
    currentAgent: null,
    currentInboxItem: null,
    hasResponded: false,
    sidebarOpen: true,
    currentInfoTab: 'tools',
    showDiff: false,
    diffData: null,
    selectedFile: null,
    ws: null,
};

// ============ WEBSOCKET ============
function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    state.ws = new WebSocket(`${protocol}//${location.host}/ws`);

    state.ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        handleMessage(msg);
    };

    state.ws.onclose = () => {
        console.log('WebSocket closed. Reconnecting...');
        setTimeout(connectWebSocket, 1000);
    };
}

function handleMessage(msg) {
    const agentId = msg.agent_id;

    // Init - Receive list of agents
    if (msg.type === 'init') {
        if (!state.agents[agentId]) {
            state.agents[agentId] = {
                project: msg.project,
                status: msg.status,
                messages: [],
                tools: [],
                todos: [],
                mode: msg.mode || 'normal',
                lastUpdate: new Date(),
                pendingInteraction: null, // 'approval', 'question', or null
            };
        }
        renderAll();
        return;
    }

    // Deletion
    if (msg.type === 'deleted') {
        delete state.agents[agentId];
        state.inbox = state.inbox.filter(i => i.agentId !== agentId);
        if (state.currentAgent === agentId) {
            showInbox();
        } else {
            renderAll();
        }
        return;
    }

    // Status Update
    if (msg.type === 'status') {
        if (state.agents[agentId]) {
            state.agents[agentId].status = msg.status;
            state.agents[agentId].lastUpdate = new Date();

            // If status is idle, clear pending interaction
            if (msg.status === 'idle') {
                state.agents[agentId].pendingInteraction = null;
            }
        }
        renderSidebar(); // Status dot update
        renderWorking();
        if (state.currentAgent === agentId) renderInputArea();
        return;
    }

    // Tool Use
    if (msg.type === 'tool') {
        if (state.agents[agentId]) {
            const tools = state.agents[agentId].tools;
            const lastTool = tools.length > 0 ? tools[tools.length - 1] : null;

            // De-dupe tools (check if same as last to avoid spam)
            const isDuplicate = lastTool &&
                lastTool.name === msg.tool.name &&
                JSON.stringify(lastTool.input) === JSON.stringify(msg.tool.input);

            if (!isDuplicate) {
                state.agents[agentId].tools.push(msg.tool);
                if (state.currentAgent === agentId) {
                    renderInfoPanel();
                }
            }
        }
        return;
    }

    // Usage
    if (msg.type === 'usage') {
        if (state.agents[agentId]) {
            const current = state.agents[agentId].usage || { input: 0, output: 0 };
            // Accumulate usage
            state.agents[agentId].usage = {
                input: (current.input || 0) + (msg.usage.input_tokens || 0),
                output: (current.output || 0) + (msg.usage.output_tokens || 0)
            };
            renderSidebar();
        }
        return;
    }

    // TODOs
    if (msg.type === 'todos') {
        if (state.agents[agentId]) {
            state.agents[agentId].todos = msg.todos;
            if (state.currentAgent === agentId) {
                renderInfoPanel();
            }
        }
        return;
    }

    // Output (Text/Completion)
    const agent = state.agents[agentId];
    if (!agent) return;

    const text = extractText(msg.content);
    if (text) {
        processAgentMessage(agent, text, msg.content.type === 'completion');
    }

    // Interrupts (Notification)
    if (msg.type === 'interrupt') {
        // Track pending interaction
        agent.pendingInteraction = msg.interrupt_type;

        addToInbox(agentId, text || '(Action required)', msg.interrupt_type);

        if (msg.content && msg.content.type === 'completion' && msg.content.text) {
            processAgentMessage(agent, msg.content.text, true);
        }
    }

    // Render
    if (state.currentAgent === agentId) {
        renderConversationMessages();
        renderInputArea(); // Re-render input to show/hide approval bar
    }
}

function processAgentMessage(agent, text, isCompletion) {
    const lastMsg = agent.messages[agent.messages.length - 1];

    if (lastMsg && lastMsg.role === 'assistant') {
        if (isCompletion) {
            lastMsg.content = text;
        } else {
            lastMsg.content += text;
        }
        lastMsg.time = new Date();
    } else {
        agent.messages.push({
            role: 'assistant',
            content: text,
            time: new Date(),
        });
    }
}

function extractText(data) {
    if (!data) return '';
    if (data.type === 'completion' && data.text) {
        return data.text;
    }
    if (data.type === 'assistant' && data.message) {
        const blocks = data.message.content || [];
        let text = '';
        for (const block of blocks) {
            if (block.type === 'text') {
                text += block.text;
            }
        }
        return text;
    }
    return '';
}

function addToInbox(agentId, content, interruptType) {
    const agent = state.agents[agentId];
    if (!agent) return;

    const lastItem = state.inbox[0];
    if (lastItem && lastItem.agentId === agentId &&
        (new Date() - lastItem.time) < 2000) {
        return;
    }

    state.inbox.unshift({
        id: Date.now().toString(),
        agentId,
        project: agent.project,
        content,
        type: interruptType || 'message',
        time: new Date(),
    });

    if (state.inbox.length > 50) {
        state.inbox = state.inbox.slice(0, 50);
    }

    renderInbox();
}

// ============ RENDERING ============
function renderAll() {
    renderSidebar();
    renderInbox();
    renderWorking();
    if (state.currentAgent) {
        renderConversation();
    }
}

function renderSidebar() {
    const container = document.getElementById('sidebar-agents');
    const agents = Object.entries(state.agents);

    if (agents.length === 0) {
        container.innerHTML = '<div class="sidebar-empty">No active agents</div>';
        return;
    }

    container.innerHTML = agents.map(([id, agent]) => {
        const isActive = state.currentAgent === id;
        const statusLabel = agent.status === 'needs_attention' ? 'Needs input' : agent.status;
        const usage = agent.usage || { input: 0, output: 0 };
        const total = usage.input + usage.output;
        const usageLabel = total > 0 ? (total / 1000).toFixed(1) + 'k' : '';

        const isHighUsage = total > 150000;

        return `
            <div class="sidebar-agent ${isActive ? 'active' : ''}" onclick="openConversation('${id}')">
                <span class="sidebar-agent-dot ${agent.status}"></span>
                <div class="sidebar-agent-info">
                    <div class="sidebar-agent-name">${escapeHtml(agent.project)}</div>
                    <div class="sidebar-agent-status">
                        ${statusLabel}
                        ${usageLabel ? `<span style="margin-left:6px; font-family:'JetBrains Mono'; opacity:0.7; ${isHighUsage ? 'color:var(--warning); font-weight:bold;' : ''}">${usageLabel}</span>` : ''}
                        ${isHighUsage ? '<span title="High context usage. Consider compacting." style="margin-left:4px; cursor:help;">⚠️</span>' : ''}
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

function toggleSidebar() {
    state.sidebarOpen = !state.sidebarOpen;
    const sidebar = document.getElementById('sidebar');
    const toggle = document.getElementById('sidebar-toggle');

    if (state.sidebarOpen) {
        sidebar.classList.add('open');
        toggle.classList.add('active');
        toggle.innerHTML = '&#9664;';
    } else {
        sidebar.classList.remove('open');
        toggle.classList.remove('active');
        toggle.innerHTML = '&#9654;';
    }
}

function switchInfoTab(tab) {
    state.currentInfoTab = tab;

    document.querySelectorAll('.info-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.info-section').forEach(s => s.classList.remove('active'));

    const activeTab = document.querySelector(`.info-tab[onclick="switchInfoTab('${tab}')"]`);
    if (activeTab) activeTab.classList.add('active');

    const activeSection = document.getElementById(`info-${tab}`);
    if (activeSection) activeSection.classList.add('active');

    renderInfoPanel();
}

function renderInfoPanel() {
    if (!state.currentAgent) return;
    const agent = state.agents[state.currentAgent];
    if (!agent) return;

    if (state.currentInfoTab === 'tools') {
        renderTools(agent);
    } else if (state.currentInfoTab === 'todos') {
        renderTodos(agent);
    }
}

function renderTools(agent) {
    const container = document.getElementById('info-tools');
    const tools = agent.tools || [];

    if (tools.length === 0) {
        container.innerHTML = '<div class="info-empty" style="color:var(--text-muted); text-align:center; padding:20px;">No tools used yet</div>';
        return;
    }

    const recentTools = tools.slice(-20).reverse();
    container.innerHTML = recentTools.map(tool => {
        const inputPreview = typeof tool.input === 'object'
            ? JSON.stringify(tool.input).slice(0, 100)
            : String(tool.input).slice(0, 100);
        return `
            <div class="tool-item">
                <div class="tool-name">${escapeHtml(tool.name)}</div>
                <div class="tool-input" style="font-family:monospace; color:var(--text-muted);">${escapeHtml(inputPreview)}...</div>
            </div>
        `;
    }).join('');
}

function renderTodos(agent) {
    const container = document.getElementById('info-todos');
    const todos = agent.todos || [];

    if (todos.length === 0) {
        container.innerHTML = '<div class="info-empty" style="color:var(--text-muted); text-align:center; padding:20px;">No todos</div>';
        return;
    }

    container.innerHTML = todos.map(todo => `
        <div class="tool-item" style="border-left: 3px solid ${getPriorityColor(todo.priority)}">
            <div style="font-weight:500; font-size:13px;">${escapeHtml(todo.content)}</div>
            <div style="font-size:10px; color:var(--text-muted); text-transform:uppercase; margin-top:4px;">${todo.priority} - ${todo.status}</div>
        </div>
    `).join('');
}

function getPriorityColor(priority) {
    switch (priority) {
        case 'high': return 'var(--error)';
        case 'medium': return 'var(--warning)';
        default: return 'var(--text-muted)';
    }
}

// Diff View
async function toggleDiffView() {
    state.showDiff = !state.showDiff;
    const btn = document.getElementById('diff-toggle');
    const main = document.getElementById('conversation-main');
    const diffView = document.getElementById('diff-view');

    if (state.showDiff) {
        btn.textContent = 'Back to Chat';
        main.style.display = 'none';
        diffView.classList.add('active');
        await loadDiff();
    } else {
        btn.textContent = 'View Diff';
        main.style.display = 'flex';
        diffView.classList.remove('active');
    }
}

async function loadDiff() {
    if (!state.currentAgent) return;

    const listContainer = document.getElementById('diff-file-list');
    const headerContainer = document.getElementById('diff-content-header');
    const bodyContainer = document.getElementById('diff-content-body');

    listContainer.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-muted)">Loading...</div>';
    headerContainer.textContent = '';
    bodyContainer.textContent = '';

    try {
        const res = await fetch(`/agents/${state.currentAgent}/diff`);
        const data = await res.json();
        state.diffData = data;

        const files = parseDiffFiles(data.diff);
        state.selectedFile = files.length > 0 ? files[0].name : null;

        renderDiffFileList(files);
        if (state.selectedFile) {
            renderDiffContent(files.find(f => f.name === state.selectedFile));
        }
    } catch (err) {
        listContainer.innerHTML = '<div style="padding:20px; text-align:center; color:var(--error)">Failed to load diff</div>';
    }
}

function parseDiffFiles(diffText) {
    if (!diffText) return [];

    const files = [];
    const chunks = diffText.split(/^diff --git /m).slice(1);

    for (const chunk of chunks) {
        const lines = chunk.split('\n');
        const headerMatch = lines[0].match(/a\/(.+?) b\/(.+)/);
        if (!headerMatch) continue;

        const fileName = headerMatch[2];
        let additions = 0, deletions = 0;
        const diffLines = [];

        for (const line of lines.slice(1)) {
            if (line.startsWith('+') && !line.startsWith('+++')) {
                additions++;
                diffLines.push({ type: 'add', text: line });
            } else if (line.startsWith('-') && !line.startsWith('---')) {
                deletions++;
                diffLines.push({ type: 'del', text: line });
            } else if (line.startsWith('@@')) {
                diffLines.push({ type: 'hunk', text: line });
            } else {
                diffLines.push({ type: 'context', text: line });
            }
        }

        files.push({ name: fileName, additions, deletions, lines: diffLines });
    }

    return files;
}

function renderDiffFileList(files) {
    const container = document.getElementById('diff-file-list');

    if (files.length === 0) {
        container.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-muted)">No changes</div>';
        return;
    }

    container.innerHTML = files.map(file => `
        <div class="diff-file-item ${file.name === state.selectedFile ? 'active' : ''}" 
             onclick="selectDiffFile('${escapeHtml(file.name)}')">
            <div style="font-weight:500;">${escapeHtml(file.name.split('/').pop())}</div>
            <div style="font-size:11px; margin-top:2px;">
                <span style="color:var(--success)">+${file.additions}</span>
                <span style="color:var(--error)">-${file.deletions}</span>
            </div>
        </div>
    `).join('');
}

function selectDiffFile(fileName) {
    state.selectedFile = fileName;
    const files = parseDiffFiles(state.diffData?.diff);
    renderDiffFileList(files);
    renderDiffContent(files.find(f => f.name === fileName));
}

function renderDiffContent(file) {
    const header = document.getElementById('diff-content-header');
    const body = document.getElementById('diff-content-body');

    if (!file) {
        header.textContent = '';
        body.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-muted)">Select a file</div>';
        return;
    }

    header.textContent = file.name;
    body.innerHTML = file.lines.map(line => {
        const cls = line.type === 'add' ? 'diff-line-add' :
            line.type === 'del' ? 'diff-line-del' :
                line.type === 'hunk' ? 'diff-line-hunk' : '';
        return `<div class="${cls}">${escapeHtml(line.text)}</div>`;
    }).join('');
}

function renderInbox() {
    const container = document.getElementById('inbox-list');

    if (state.inbox.length === 0) {
        const hasAgents = Object.keys(state.agents).length > 0;
        const message = hasAgents
            ? 'Agents are working. You\'ll be notified when they need you.'
            : 'Add a project to get started';
        container.innerHTML = `
            <div class="inbox-empty">
                <h2>All clear</h2>
                <p>${message}</p>
            </div>
        `;
        return;
    }

    container.innerHTML = state.inbox.map(item => {
        const timeAgo = formatTimeAgo(item.time);
        const typeLabel = getTypeLabel(item.type);
        const statusColor = getStatusColor(item.type);

        return `
            <div class="inbox-item" onclick="openConversation('${item.agentId}', '${item.id}')" style="border-left: 3px solid ${statusColor}">
                <div class="inbox-item-header">
                    <div class="inbox-item-project">
                        <span style="width:8px; height:8px; border-radius:50%; background:${statusColor}; display:inline-block;"></span>
                        ${escapeHtml(item.project)}
                    </div>
                    <span class="inbox-item-time">${timeAgo}</span>
                </div>
                <div class="inbox-item-content">${escapeHtml(item.content)}</div>
                <div style="font-size:11px; color:var(--text-muted); margin-top:12px; text-transform:uppercase; letter-spacing:0.05em;">${typeLabel}</div>
            </div>
        `;
    }).join('');
}

function renderWorking() {
    // Redundant
}

function renderConversation() {
    const agent = state.agents[state.currentAgent];
    if (!agent) return;

    document.getElementById('conversation-project').textContent = agent.project;
    renderConversationMessages();
    renderInputArea();
    renderInfoPanel();
}

function renderConversationMessages() {
    const agent = state.agents[state.currentAgent];
    if (!agent) return;

    const container = document.getElementById('conversation-messages');
    container.innerHTML = agent.messages.map(msg => `
        <div class="message ${msg.role}">
            <div class="message-role">${msg.role}</div>
            <div class="message-content markdown-body">${marked.parse(msg.content)}</div>
        </div>
    `).join('');

    // Highlight code blocks
    container.querySelectorAll('pre code').forEach((block) => {
        hljs.highlightElement(block);
    });

    // Auto-scroll
    container.scrollTop = container.scrollHeight;
}

// Replaces the input box with Approval Bar if needed
function renderInputArea() {
    const agent = state.agents[state.currentAgent];
    if (!agent) return;

    const inputContainer = document.querySelector('.conversation-input');

    if (agent.pendingInteraction === 'approval') {
        // Try to find the pending tool
        const lastTool = agent.tools && agent.tools.length > 0 ? agent.tools[agent.tools.length - 1] : null;
        let commandPreview = "Action pending approval";

        if (lastTool) {
            if (lastTool.name === 'Bash') commandPreview = `$> ${lastTool.input.command}`;
            else if (lastTool.name === 'Edit') commandPreview = `Edit file: ${lastTool.input.path}`;
            else if (lastTool.name === 'Write') commandPreview = `Write file: ${lastTool.input.path}`;
            else commandPreview = `Use tool: ${lastTool.name}`;
        }

        inputContainer.innerHTML = `
            <div class="approval-bar">
                <div class="approval-message" style="flex-direction: column; align-items: flex-start; width: 100%;">
                    <span style="color:var(--accent); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:0.05em;">⚠ Approval Required</span>
                    <div style="font-weight:500; color:var(--text-primary); font-family:'JetBrains Mono', monospace; font-size:13px; margin-top:6px; background:rgba(0,0,0,0.2); padding:8px; border-radius:4px; width:100%; box-sizing:border-box; white-space: pre-wrap; word-break: break-word; max-height: 300px; overflow-y: auto;">${escapeHtml(commandPreview)}</div>
                </div>
                <div class="approval-actions">
                    <button class="btn btn-reject" onclick="rejectAction()">Reject</button>
                    <button class="btn btn-approve" onclick="approveAction()">Yes</button>
                    <button class="btn btn-approve" style="background:var(--accent);" onclick="approveAlwaysAction()">Yes Always</button>
                </div>
            </div>
        `;
    } else {
        // Standard input
        inputContainer.innerHTML = `
            <div class="input-wrapper">
                <input type="text" id="conversation-input" placeholder="Give instructions..." onkeypress="handleConversationKeypress(event)">
                <button class="btn btn-primary" onclick="sendConversationMessage()">Send</button>
            </div>
        `;
        // Restore focus if needed
        const input = document.getElementById('conversation-input');
        if (input) input.focus();
    }
}

// ============ ACTIONS ============
function openConversation(agentId, inboxItemId) {
    state.currentAgent = agentId;
    state.hasResponded = false;

    if (inboxItemId) {
        state.currentInboxItem = state.inbox.find(i => i.id === inboxItemId);
        state.inbox = state.inbox.filter(i => i.id !== inboxItemId);
    } else {
        state.currentInboxItem = null;
    }

    document.getElementById('inbox-view').classList.remove('active');
    document.getElementById('conversation-view').classList.add('active');

    renderConversation();

    // Update sidebar selection
    renderSidebar();
}

function showInbox() {
    if (state.currentInboxItem && !state.hasResponded) {
        state.inbox.unshift(state.currentInboxItem);
    }

    state.currentAgent = null;
    state.currentInboxItem = null;
    state.hasResponded = false;
    state.showDiff = false;
    state.diffData = null;
    state.selectedFile = null;

    const main = document.getElementById('conversation-main');
    const diffView = document.getElementById('diff-view');
    main.style.display = 'flex';
    diffView.classList.remove('active');
    document.getElementById('diff-toggle').textContent = 'View Diff';

    document.getElementById('conversation-view').classList.remove('active');
    document.getElementById('inbox-view').classList.add('active');

    renderAll();
}

function sendConversationMessage(textOverride) {
    let content = textOverride;

    if (!content) {
        const input = document.getElementById('conversation-input');
        if (input) content = input.value.trim();
    }

    if (!content && content !== "") return; // Allow empty strings if passed explicitly (e.g. just Enter)
    if (!state.currentAgent) return;

    const agent = state.agents[state.currentAgent];
    if (!agent) return;

    state.hasResponded = true;
    state.currentInboxItem = null;
    agent.pendingInteraction = null; // Clear pending state

    // Optimistic UI for User Message
    if (content) {
        agent.messages.push({
            role: 'user',
            content: content,
            time: new Date(),
        });
    }

    state.ws.send(JSON.stringify({
        agent_id: state.currentAgent,
        content: content,
    }));

    if (document.getElementById('conversation-input')) {
        document.getElementById('conversation-input').value = '';
    }

    // Auto-return to inbox
    setTimeout(() => {
        showInbox();
    }, 300);
}

function approveAction() {
    // Send "y" to approve
    sendConversationMessage("y");
}

function approveAlwaysAction() {
    // Send "always" or similar to approve this and future interactions
    // Assuming the underlying agent understands "always" or we just interpret it as "y" for now
    // If the agent framework supports it, usually "always" or "all" works? 
    // Let's send "always" as requested.
    sendConversationMessage("always");
}

function rejectAction() {
    // For rejection, we might want to ask for a reason. 
    // For now, let's just send "n" or let user type.
    // If they click reject, let's assume "n"
    const reason = prompt("Enter rejection reason (optional), or cancel to just send 'n':");
    if (reason === null) {
        // Just send 'n'
        sendConversationMessage("n");
    } else {
        sendConversationMessage(reason || "n");
    }
}

function handleConversationKeypress(event) {
    if (event.key === 'Enter') {
        sendConversationMessage();
    }
}

// ============ PROJECT MODAL ============
async function openProjectModal() {
    const res = await fetch('/projects');
    const data = await res.json();

    const container = document.getElementById('project-list');
    const runningProjects = new Set(
        Object.values(state.agents).map(a => a.project)
    );

    container.innerHTML = data.projects.map(p => {
        const isRunning = runningProjects.has(p);
        return `
            <div class="project-option ${isRunning ? 'running' : ''}" onclick="selectProject('${p}')">
                <span class="project-option-name" style="font-weight:500">${escapeHtml(p)}</span>
                <span class="project-option-status" style="font-size:12px">${isRunning ? 'Running' : 'Click to start'}</span>
            </div>
        `;
    }).join('');

    document.getElementById('project-modal').classList.add('active');
}

function closeProjectModal() {
    document.getElementById('project-modal').classList.remove('active');
}

async function closeAgent(agentId) {
    state.inbox = state.inbox.filter(i => i.agentId !== agentId);
    delete state.agents[agentId];
    await fetch(`/agents/${agentId}`, { method: 'DELETE' });
}

async function closeCurrentAgent() {
    if (state.currentAgent) {
        state.currentInboxItem = null;
        await closeAgent(state.currentAgent);
        showInbox();
    }
}

async function selectProject(project) {
    const existing = Object.entries(state.agents).find(([id, a]) => a.project === project);
    if (existing) {
        closeProjectModal();
        openConversation(existing[0]);
        return;
    }

    const mode = document.getElementById('mode-select').value;

    const tempId = 'temp_' + Date.now();

    state.agents[tempId] = {
        project: project,
        status: 'working',
        messages: [],
        tools: [],
        todos: [],
        mode: mode,
        pendingInteraction: null,
    };
    renderAll();

    closeProjectModal();

    const res = await fetch('/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project, mode }),
    });

    const data = await res.json();

    delete state.agents[tempId];

    if (data.agent_id) {
        state.agents[data.agent_id] = {
            project: data.project,
            status: 'working',
            messages: [],
            tools: [],
            todos: [],
            mode: mode,
        };
        renderAll();
    }
}

// ============ UTILITIES ============
function getStatusColor(type) {
    switch (type) {
        case 'question': return 'var(--warning)';
        case 'approval': return 'var(--error)';
        case 'complete': return 'var(--success)';
        case 'update': return 'var(--accent)';
        case 'status': return 'var(--accent)';
        default: return 'var(--accent)';
    }
}

function getTypeLabel(type) {
    switch (type) {
        case 'question': return 'Needs Input';
        case 'approval': return 'Approval Required';
        case 'plan': return 'Plan Ready';
        case 'complete': return 'Completed';
        case 'update': return 'Update';
        case 'status': return 'Status';
        default: return 'Message';
    }
}

function formatTimeAgo(date) {
    const seconds = Math.floor((new Date() - new Date(date)) / 1000);
    if (seconds < 60) return 'just now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.floor(hours / 24)}d ago`;
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ============ INIT ============
connectWebSocket();
document.getElementById('sidebar').classList.add('open');
