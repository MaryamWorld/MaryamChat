/* Conversation list, user search, message history and real-time message delivery. */
(function () {
  const { api, el, toast, fmtTime, fmtDay, sameDay, initial, me } = window.CW;
  const $ = (id) => document.getElementById(id);

  const socket = io();
  window.CW.socket = socket;

  let conversations = [];
  let activeId = null;
  let messages = [];
  let hasMore = false;
  let seen = new Set();
  let firstConnect = true;

  const convById = (id) => conversations.find((c) => c.id === id);
  window.CW.activePeer = () => {
    const c = convById(activeId);
    return c ? { id: c.peer.id, name: c.peer.display_name } : null;
  };

  /* ---------- conversation list ---------- */
  async function loadConversations() {
    try {
      conversations = (await api('/api/conversations')).conversations;
      renderConversations();
    } catch (e) {
      $('conversation-list').replaceChildren(el('div', 'empty error-text', e.message));
    }
  }

  function renderConversations() {
    const list = $('conversation-list');
    if (!conversations.length) {
      list.replaceChildren(el('div', 'empty', 'No conversations yet. Search for a user above to start one.'));
      return;
    }
    list.replaceChildren(...conversations.map((c) => {
      const row = el('button', 'row' + (c.id === activeId ? ' active' : ''));
      row.type = 'button';
      row.setAttribute('role', 'listitem');
      row.append(avatar(c.peer.display_name, c.peer.online));
      const text = el('div', 'row-text');
      text.append(el('strong', '', c.peer.display_name),
                  el('span', 'muted preview', c.last_body || 'No messages yet'));
      row.append(text);
      const meta = el('div', 'row-meta');
      if (c.last_at) meta.append(el('span', 'muted small', fmtTime(c.last_at)));
      if (c.unread > 0) meta.append(el('span', 'badge', String(c.unread)));
      row.append(meta);
      row.addEventListener('click', () => openConversation(c.id));
      return row;
    }));
  }

  function avatar(name, online) {
    const a = el('span', 'avatar', initial(name));
    if (online) a.classList.add('online');
    return a;
  }

  /* ---------- user search ---------- */
  let searchTimer = null;
  $('search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    const q = e.target.value.trim();
    if (!q) { hideSearch(); return; }
    searchTimer = setTimeout(() => runSearch(q), 250);
  });

  function hideSearch() { $('search-results').hidden = true; $('conversation-list').hidden = false; }

  async function runSearch(q) {
    const box = $('search-results');
    box.hidden = false;
    $('conversation-list').hidden = true;
    box.replaceChildren(el('div', 'empty', 'Searching…'));
    try {
      const { users } = await api('/api/users?q=' + encodeURIComponent(q));
      if ($('search').value.trim() !== q) return; // stale response
      if (!users.length) { box.replaceChildren(el('div', 'empty', 'No users found.')); return; }
      box.replaceChildren(...users.map((u) => {
        const row = el('button', 'row');
        row.type = 'button';
        row.append(avatar(u.display_name, u.online));
        const text = el('div', 'row-text');
        text.append(el('strong', '', u.display_name), el('span', 'muted preview', '@' + u.username));
        row.append(text);
        row.addEventListener('click', () => startConversation(u.id));
        return row;
      }));
    } catch (e) {
      box.replaceChildren(el('div', 'empty error-text', e.message));
    }
  }

  async function startConversation(userId) {
    try {
      const { conversation } = await api('/api/conversations', { method: 'POST', body: { user_id: userId } });
      if (!convById(conversation.id)) conversations.unshift(conversation);
      $('search').value = '';
      hideSearch();
      renderConversations();
      openConversation(conversation.id);
    } catch (e) { toast(e.message, 'error'); }
  }

  /* ---------- message panel ---------- */
  async function openConversation(id) {
    const c = convById(id);
    if (!c) return;
    activeId = id;
    seen = new Set();
    messages = [];
    $('app').classList.add('show-chat');
    $('empty-chat').hidden = true;
    $('chat-view').hidden = false;
    $('peer-name').textContent = c.peer.display_name;
    $('peer-status').textContent = '@' + c.peer.username;
    $('peer-avatar').textContent = initial(c.peer.display_name);
    $('messages').replaceChildren(el('div', 'empty', 'Loading messages…'));
    renderConversations();
    $('message-input').focus();
    try {
      const data = await api(`/api/conversations/${id}/messages`);
      if (activeId !== id) return;
      messages = data.messages;
      hasMore = data.has_more;
      messages.forEach((m) => seen.add(m.id));
      c.unread = 0;
      renderConversations();
      renderMessages(true);
    } catch (e) {
      $('messages').replaceChildren(el('div', 'empty error-text', e.message));
    }
  }

  function renderMessages(scrollToBottom, keepOffsetFrom) {
    const box = $('messages');
    const nodes = [];
    if (hasMore) {
      const more = el('button', 'btn link', 'Load older messages');
      more.type = 'button';
      more.addEventListener('click', loadOlder);
      nodes.push(more);
    }
    if (!messages.length) nodes.push(el('div', 'empty', 'No messages yet. Say hello!'));
    let prev = null;
    for (const m of messages) {
      if (!prev || !sameDay(prev.created_at, m.created_at)) nodes.push(el('div', 'day', fmtDay(m.created_at)));
      const bubble = el('div', 'msg ' + (m.sender_id === me.id ? 'mine' : 'theirs'));
      bubble.append(el('div', 'text', m.body), el('div', 'time', fmtTime(m.created_at)));
      nodes.push(bubble);
      prev = m;
    }
    box.replaceChildren(...nodes);
    if (scrollToBottom) box.scrollTop = box.scrollHeight;
    else if (keepOffsetFrom !== undefined) box.scrollTop = box.scrollHeight - keepOffsetFrom;
  }

  async function loadOlder() {
    if (!messages.length) return;
    const id = activeId;
    const box = $('messages');
    const oldHeight = box.scrollHeight;
    try {
      const data = await api(`/api/conversations/${id}/messages?before=${messages[0].id}`);
      if (activeId !== id) return;
      data.messages.forEach((m) => seen.add(m.id));
      messages = data.messages.concat(messages);
      hasMore = data.has_more;
      renderMessages(false, oldHeight);
    } catch (e) { toast(e.message, 'error'); }
  }

  function addMessage(m) {
    if (seen.has(m.id)) return;
    seen.add(m.id);
    messages.push(m);
    const box = $('messages');
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
    renderMessages(nearBottom || m.sender_id === me.id);
  }

  /* ---------- sending ---------- */
  const input = $('message-input');
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('composer').requestSubmit(); }
  });

  $('composer').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = input.value.trim();
    if (!body || activeId === null) return;
    const id = activeId;
    $('btn-send').disabled = true;
    try {
      const { message } = await api(`/api/conversations/${id}/messages`, { method: 'POST', body: { body } });
      input.value = '';
      input.style.height = 'auto';
      if (activeId === id) addMessage(message);
      touchConversation(message);
    } catch (err) {
      toast(err.message, 'error'); // the typed text stays in the box so nothing is lost
    } finally {
      $('btn-send').disabled = false;
      input.focus();
    }
  });

  function touchConversation(m) {
    const c = convById(m.conversation_id);
    if (!c) return false;
    c.last_body = m.body;
    c.last_at = m.created_at;
    c.updated_at = m.created_at;
    if (m.sender_id !== me.id && m.conversation_id !== activeId) c.unread += 1;
    conversations.sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
    renderConversations();
    return true;
  }

  /* ---------- real-time events ---------- */
  socket.on('message:new', async (m) => {
    if (!convById(m.conversation_id)) await loadConversations(); // first message from a new contact
    else touchConversation(m);
    if (m.conversation_id === activeId) {
      addMessage(m);
      if (m.sender_id !== me.id) {
        api(`/api/conversations/${activeId}/read`, { method: 'POST', body: {} }).catch(() => {});
      }
    }
  });

  socket.on('connect', async () => {
    $('conn-banner').hidden = true;
    if (firstConnect) { firstConnect = false; return; }
    // Re-sync after a dropped connection so no message is missed.
    await loadConversations();
    if (activeId !== null) {
      try {
        const data = await api(`/api/conversations/${activeId}/messages`);
        messages = data.messages; hasMore = data.has_more;
        seen = new Set(messages.map((m) => m.id));
        renderMessages(true);
      } catch (e) { /* shown on next action */ }
    }
  });
  socket.on('disconnect', () => { $('conn-banner').hidden = false; });
  socket.on('connect_error', () => { $('conn-banner').hidden = false; });

  /* ---------- mobile navigation ---------- */
  $('btn-back').addEventListener('click', () => {
    $('app').classList.remove('show-chat');
    activeId = null;
    $('chat-view').hidden = true;
    $('empty-chat').hidden = false;
    renderConversations();
  });

  loadConversations();
})();
