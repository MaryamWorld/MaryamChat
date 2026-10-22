/* Audio/video calls: WebRTC media + Socket.IO signaling (see call:* events in app.py). */
(function () {
  const { api, toast, initial } = window.CW;
  const socket = window.CW.socket;
  const $ = (id) => document.getElementById(id);
  const RING_TIMEOUT_MS = 45000;
  const UNSTABLE_GRACE_MS = 10000;

  let call = null;      // the call this tab is in: { id, kind, role, peer, pc, local, ice, pending, timers }
  let incoming = null;  // an unanswered incoming call: { id, kind, from }
  let ringtone = null;

  const REASONS = {
    rejected: 'Call declined.',
    cancelled: 'Call cancelled.',
    ended: 'Call ended.',
    disconnected: 'The other person lost connection.',
  };

  /* ---------- helpers ---------- */
  function mediaErrorMessage(e) {
    switch (e && e.name) {
      case 'NotAllowedError': return 'Microphone/camera permission was denied. Allow access in your browser and try again.';
      case 'NotFoundError': return 'No microphone or camera was found on this device.';
      case 'NotReadableError': return 'Your microphone or camera is being used by another application.';
      default: return 'Could not access your microphone or camera.';
    }
  }

  function getMedia(kind) {
    return navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
      video: kind === 'video' ? { width: { ideal: 1280 }, height: { ideal: 720 } } : false,
    });
  }

  const stopStream = (s) => s && s.getTracks().forEach((t) => t.stop());
  const setStatus = (t) => { $('call-status').textContent = t; };

  function showOverlay(name, kind) {
    $('call-name').textContent = name;
    $('call-avatar').textContent = initial(name);
    $('call-overlay').classList.toggle('audio-only', kind !== 'video');
    $('btn-camera').hidden = kind !== 'video';
    setPressed('btn-mute', false, 'Mute', 'Unmute');
    setPressed('btn-camera', false, 'Camera off', 'Camera on');
    $('call-overlay').hidden = false;
  }

  function setPressed(id, on, offLabel, onLabel) {
    const b = $(id);
    b.setAttribute('aria-pressed', String(on));
    b.classList.toggle('off', on);
    b.textContent = on ? onLabel : offLabel;
  }

  function startRingtone() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = new Ctx();
      const beep = () => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.frequency.value = 440;
        gain.gain.value = 0.08;
        osc.connect(gain); gain.connect(ctx.destination);
        osc.start(); osc.stop(ctx.currentTime + 0.4);
      };
      beep();
      ringtone = { ctx, id: setInterval(beep, 2000) };
    } catch (e) { /* audio may be blocked until the user interacts; the popup still shows */ }
  }

  function stopRingtone() {
    if (!ringtone) return;
    clearInterval(ringtone.id);
    ringtone.ctx.close().catch(() => {});
    ringtone = null;
  }

  function hideIncoming() {
    stopRingtone();
    incoming = null;
    $('incoming-modal').hidden = true;
  }

  /* Releases everything: tracks, peer connection, timers and UI. Safe to call repeatedly. */
  function cleanup() {
    const c = call;
    call = null;
    if (c) {
      clearTimeout(c.timers.ring);
      clearTimeout(c.timers.unstable);
      clearInterval(c.timers.duration);
      if (c.pc) {
        c.pc.onicecandidate = c.pc.ontrack = c.pc.onconnectionstatechange = null;
        c.pc.close();
      }
      stopStream(c.local);
    }
    $('remote-video').srcObject = null;
    $('local-video').srcObject = null;
    $('call-overlay').hidden = true;
    setStatus('');
  }

  function hangup() {
    if (!call) return;
    if (call.id) socket.emit('call:end', { call_id: call.id });
    cleanup();
  }

  function fail(message) {
    toast(message, 'error');
    hangup();
  }

  /* ---------- peer connection ---------- */
  function createPeer() {
    const pc = new RTCPeerConnection({ iceServers: call.ice.iceServers });
    call.pc = pc;
    call.local.getTracks().forEach((t) => pc.addTrack(t, call.local));

    pc.onicecandidate = (e) => {
      if (e.candidate && call && call.pc === pc) {
        socket.emit('call:signal', { call_id: call.id, data: { type: 'candidate', candidate: e.candidate.toJSON() } });
      }
    };
    pc.ontrack = (e) => {
      if (call && call.pc === pc) $('remote-video').srcObject = e.streams[0];
    };
    pc.onconnectionstatechange = () => {
      if (!call || call.pc !== pc) return;
      const state = pc.connectionState;
      if (state === 'connected') {
        clearTimeout(call.timers.unstable);
        if (!call.timers.duration) startTimer();
      } else if (state === 'disconnected') {
        setStatus('Connection unstable…');
        call.timers.unstable = setTimeout(() => {
          if (call && call.pc === pc && pc.connectionState !== 'connected') fail('Connection lost.');
        }, UNSTABLE_GRACE_MS);
      } else if (state === 'failed') {
        fail('Could not establish the call. A TURN server may be required for this network.');
      }
    };
  }

  function startTimer() {
    const started = Date.now();
    const tick = () => {
      const s = Math.floor((Date.now() - started) / 1000);
      setStatus(`${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`);
    };
    tick();
    call.timers.duration = setInterval(tick, 1000);
  }

  async function flushPending() {
    const queued = call.pending.splice(0);
    for (const cand of queued) await call.pc.addIceCandidate(cand);
  }

  /* ---------- outgoing call ---------- */
  async function startCall(kind) {
    if (call || incoming) { toast('You are already in a call.'); return; }
    const peer = window.CW.activePeer();
    if (!peer) return;
    if (!window.isSecureContext || !navigator.mediaDevices || !window.RTCPeerConnection) {
      toast('Calls need a secure connection. Open the site at http://localhost:5000 or use HTTPS.', 'error');
      return;
    }
    if (!socket.connected) { toast('Not connected to the server yet. Try again in a moment.', 'error'); return; }

    const mine = { id: null, kind, role: 'caller', peer, pc: null, local: null, ice: null, pending: [], timers: {} };
    call = mine;
    showOverlay(peer.name, kind);
    setStatus('Requesting microphone' + (kind === 'video' ? ' and camera' : '') + '…');
    let stream, ice;
    try {
      stream = await getMedia(kind);
      ice = await api('/api/ice');
    } catch (e) {
      stopStream(stream);
      if (call === mine) { toast(e instanceof DOMException ? mediaErrorMessage(e) : e.message, 'error'); cleanup(); }
      return;
    }
    if (call !== mine) { stopStream(stream); return; } // cancelled while waiting for permission
    mine.local = stream;
    mine.ice = ice;
    $('local-video').srcObject = stream;
    setStatus('Calling…');
    socket.emit('call:invite', { callee_id: peer.id, kind });
  }

  socket.on('call:ringing', ({ call_id }) => {
    if (!call || call.role !== 'caller' || call.id) return;
    call.id = call_id;
    setStatus('Ringing…');
    call.timers.ring = setTimeout(() => { toast('No answer.'); hangup(); }, RING_TIMEOUT_MS);
  });

  socket.on('call:unavailable', () => {
    if (call && !call.id) { toast(`${call.peer.name} is offline right now.`); cleanup(); }
  });
  socket.on('call:busy', () => {
    if (call && !call.id) { toast(`${call.peer.name} is on another call.`); cleanup(); }
  });
  socket.on('call:error', ({ message }) => {
    toast(message, 'error');
    if (call && !call.id) cleanup();
  });

  socket.on('call:accepted', async ({ call_id }) => {
    if (!call || call.id !== call_id || call.pc) return;
    clearTimeout(call.timers.ring);
    setStatus('Connecting…');
    try {
      createPeer();
      const pc = call.pc;
      await pc.setLocalDescription(await pc.createOffer());
      socket.emit('call:signal', { call_id, data: pc.localDescription.toJSON() });
    } catch (e) {
      console.error(e);
      fail('Could not start the connection.');
    }
  });

  /* ---------- incoming call ---------- */
  socket.on('call:incoming', ({ call_id, kind, from }) => {
    if (call || incoming) return; // the server also rejects overlapping calls as "busy"
    incoming = { id: call_id, kind, from };
    $('incoming-name').textContent = from.display_name;
    $('incoming-avatar').textContent = initial(from.display_name);
    $('incoming-kind').textContent = kind === 'video' ? 'Incoming video call…' : 'Incoming audio call…';
    $('incoming-modal').hidden = false;
    startRingtone();
  });

  async function acceptIncoming() {
    const inc = incoming;
    if (!inc) return;
    if (!window.isSecureContext || !navigator.mediaDevices || !window.RTCPeerConnection) {
      toast('Calls need a secure connection. Open the site at http://localhost:5000 or use HTTPS.', 'error');
      return rejectIncoming();
    }
    hideIncoming();
    const mine = { id: inc.id, kind: inc.kind, role: 'callee', peer: { id: inc.from.id, name: inc.from.display_name },
                   pc: null, local: null, ice: null, pending: [], timers: {} };
    call = mine;
    showOverlay(mine.peer.name, mine.kind);
    setStatus('Connecting…');
    let stream, ice;
    try {
      stream = await getMedia(inc.kind);
      ice = await api('/api/ice');
    } catch (e) {
      stopStream(stream);
      if (call === mine) {
        toast(e instanceof DOMException ? mediaErrorMessage(e) : e.message, 'error');
        socket.emit('call:reject', { call_id: inc.id });
        cleanup();
      }
      return;
    }
    if (call !== mine) { stopStream(stream); return; } // caller hung up while we were waiting
    mine.local = stream;
    mine.ice = ice;
    $('local-video').srcObject = stream;
    createPeer();                       // ready to receive the offer before we announce acceptance
    socket.emit('call:accept', { call_id: mine.id });
  }

  function rejectIncoming() {
    if (!incoming) return;
    socket.emit('call:reject', { call_id: incoming.id });
    hideIncoming();
  }

  /* ---------- signaling & termination ---------- */
  socket.on('call:signal', async ({ call_id, data }) => {
    if (!call || call.id !== call_id || !call.pc || !data) return;
    const pc = call.pc;
    try {
      if (data.type === 'offer') {
        await pc.setRemoteDescription(data);
        await flushPending();
        await pc.setLocalDescription(await pc.createAnswer());
        socket.emit('call:signal', { call_id, data: pc.localDescription.toJSON() });
        setStatus('Connecting…');
      } else if (data.type === 'answer') {
        await pc.setRemoteDescription(data);
        await flushPending();
      } else if (data.type === 'candidate') {
        if (pc.remoteDescription) await pc.addIceCandidate(data.candidate);
        else call.pending.push(data.candidate);
      }
    } catch (e) {
      console.error(e);
      fail('Connection setup failed.');
    }
  });

  socket.on('call:ended', ({ call_id, reason }) => {
    if (incoming && incoming.id === call_id) {
      const from = incoming.from.display_name;
      hideIncoming();
      if (reason !== 'answered_elsewhere' && reason !== 'rejected') toast(`Missed call from ${from}.`);
      return;
    }
    if (call && call.id === call_id) {
      toast(REASONS[reason] || 'Call ended.');
      cleanup();
    }
  });

  socket.on('disconnect', () => {
    if (incoming) hideIncoming();
    if (call) { toast('Connection to the server was lost. The call ended.', 'error'); cleanup(); }
  });

  /* ---------- controls ---------- */
  $('btn-audio-call').addEventListener('click', () => startCall('audio'));
  $('btn-video-call').addEventListener('click', () => startCall('video'));
  $('btn-accept').addEventListener('click', acceptIncoming);
  $('btn-reject').addEventListener('click', rejectIncoming);
  $('btn-hangup').addEventListener('click', hangup);

  $('btn-mute').addEventListener('click', () => {
    if (!call || !call.local) return;
    const tracks = call.local.getAudioTracks();
    const muted = tracks.length ? tracks[0].enabled : false; // enabled -> about to mute
    tracks.forEach((t) => { t.enabled = !muted; });
    setPressed('btn-mute', muted, 'Mute', 'Unmute');
  });

  $('btn-camera').addEventListener('click', () => {
    if (!call || !call.local) return;
    const tracks = call.local.getVideoTracks();
    if (!tracks.length) return;
    const off = tracks[0].enabled;
    tracks.forEach((t) => { t.enabled = !off; });
    setPressed('btn-camera', off, 'Camera off', 'Camera on');
  });

  window.addEventListener('beforeunload', () => { if (call && call.id) socket.emit('call:end', { call_id: call.id }); });
})();
