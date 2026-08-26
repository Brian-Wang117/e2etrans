(function () {
    'use strict';

    const state = {
        ua: null,
        currentSession: null,
        isMuted: false,
        isSpeakerOn: false,
        callTimerInterval: null,
        callStartTime: null,
        history: [],
        localStream: null,
        remoteAudio: null,
        processingEnabled: false,
        processedLocalStream: null,
        processedRemoteStream: null,
        originalLocalTrack: null
    };

    var STATUS_CONFIRMED = 'confirmed';

    const $ = (id) => document.getElementById(id);

    const els = {
        statusDot: $('statusDot'),
        statusText: $('statusText'),
        configPanel: $('configPanel'),
        callPanel: $('callPanel'),
        sipServer: $('sipServer'),
        sipPort: $('sipPort'),
        sipTransport: $('sipTransport'),
        sipUser: $('sipUser'),
        sipDomain: $('sipDomain'),
        sipPassword: $('sipPassword'),
        btnRegister: $('btnRegister'),
        btnUnregister: $('btnUnregister'),
        callerAvatar: $('callerAvatar'),
        callerName: $('callerName'),
        callerNumber: $('callerNumber'),
        callStatus: $('callStatus'),
        callTimer: $('callTimer'),
        incomingCallPanel: $('incomingCallPanel'),
        incomingNumber: $('incomingNumber'),
        btnReject: $('btnReject'),
        btnAnswer: $('btnAnswer'),
        callControls: $('callControls'),
        btnMute: $('btnMute'),
        btnSpeaker: $('btnSpeaker'),
        btnKeypad: $('btnKeypad'),
        btnTransfer: $('btnTransfer'),
        keypadPanel: $('keypadPanel'),
        dialNumber: $('dialNumber'),
        btnClear: $('btnClear'),
        btnDial: $('btnDial'),
        btnHangup: $('btnHangup'),
        transferPanel: $('transferPanel'),
        transferNumber: $('transferNumber'),
        btnTransferConfirm: $('btnTransferConfirm'),
        btnTransferCancel: $('btnTransferCancel'),
        historyPanel: $('historyPanel'),
        historyList: $('historyList'),
        btnClearHistory: $('btnClearHistory'),
        enableProcessing: $('enableProcessing')
    };

    document.addEventListener('DOMContentLoaded', init);

    function init() {
        if (typeof JsSIP === 'undefined') {
            console.error('JsSIP 未加载，请检查网络连接或 CDN 链接');
            alert('JsSIP 库加载失败，请检查网络连接后刷新页面');
            return;
        }
        console.log('JsSIP 已加载，版本:', JsSIP.version);
        loadSavedConfig();
        bindEvents();
        loadHistory();
        renderHistory();
        ensureAudioElement();
    }

    function ensureAudioElement() {
        state.remoteAudio = document.getElementById('remoteAudio');
        if (!state.remoteAudio) {
            state.remoteAudio = document.createElement('audio');
            state.remoteAudio.id = 'remoteAudio';
            state.remoteAudio.autoplay = true;
            state.remoteAudio.playsInline = true;
            state.remoteAudio.style.display = 'none';
            document.body.appendChild(state.remoteAudio);
        }
    }

    function loadSavedConfig() {
        try {
            const saved = localStorage.getItem('softphone_config');
            if (saved) {
                const cfg = JSON.parse(saved);
                els.sipServer.value = cfg.server || els.sipServer.value;
                els.sipPort.value = cfg.port || els.sipPort.value;
                els.sipTransport.value = cfg.transport || els.sipTransport.value;
                els.sipUser.value = cfg.user || els.sipUser.value;
                els.sipDomain.value = cfg.domain || els.sipDomain.value;
                els.sipPassword.value = cfg.password || '';
                if (els.enableProcessing) {
                    els.enableProcessing.checked = !!cfg.processingEnabled;
                    state.processingEnabled = !!cfg.processingEnabled;
                }
            }
        } catch (e) { /* ignore */ }
    }

    function saveConfig() {
        const cfg = {
            server: els.sipServer.value.trim(),
            port: els.sipPort.value.trim(),
            transport: els.sipTransport.value,
            user: els.sipUser.value.trim(),
            domain: els.sipDomain.value.trim(),
            password: els.sipPassword.value,
            processingEnabled: !!(els.enableProcessing && els.enableProcessing.checked)
        };
        localStorage.setItem('softphone_config', JSON.stringify(cfg));
    }

    function getConfig() {
        return {
            server: els.sipServer.value.trim(),
            port: els.sipPort.value.trim() || '5060',
            transport: els.sipTransport.value,
            user: els.sipUser.value.trim(),
            domain: els.sipDomain.value.trim(),
            password: els.sipPassword.value,
            processingEnabled: !!(els.enableProcessing && els.enableProcessing.checked)
        };
    }

    function bindEvents() {
        els.btnRegister.addEventListener('click', register);
        els.btnUnregister.addEventListener('click', unregister);
        els.btnReject.addEventListener('click', rejectIncoming);
        els.btnAnswer.addEventListener('click', answerIncoming);
        els.btnMute.addEventListener('click', toggleMute);
        els.btnSpeaker.addEventListener('click', toggleSpeaker);
        els.btnKeypad.addEventListener('click', toggleKeypad);
        els.btnTransfer.addEventListener('click', toggleTransfer);
        els.btnClear.addEventListener('click', clearDialInput);
        els.btnDial.addEventListener('click', dial);
        els.btnHangup.addEventListener('click', hangup);
        els.btnTransferConfirm.addEventListener('click', confirmTransfer);
        els.btnTransferCancel.addEventListener('click', cancelTransfer);
        els.btnClearHistory.addEventListener('click', clearHistory);

        if (els.enableProcessing) {
            els.enableProcessing.addEventListener('change', function() {
                state.processingEnabled = els.enableProcessing.checked;
                saveConfig();
                console.log('[媒体处理] 翻译管线 ' + (state.processingEnabled ? '已启用' : '已禁用'));
            });
        }

        document.querySelectorAll('.key').forEach(key => {
            key.addEventListener('click', () => {
                var digit = key.dataset.key;
                addDigit(digit);
                if (state.currentSession && state.currentSession.status === STATUS_CONFIRMED) {
                    sendDTMF(digit);
                }
            });
        });

        els.dialNumber.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && els.dialNumber.value.trim()) {
                if (state.currentSession && state.currentSession.status === STATUS_CONFIRMED) {
                    sendDTMF(els.dialNumber.value.trim().slice(-1));
                } else {
                    dial();
                }
            }
        });
    }

    function setStatus(status) {
        els.statusDot.className = 'status-dot ' + status;
        const textMap = {
            disconnected: '未连接',
            connecting: '连接中...',
            connected: '已连接',
            error: '连接错误'
        };
        els.statusText.textContent = textMap[status] || status;
    }

    function showCallPanel() {
        els.configPanel.style.display = 'none';
        els.callPanel.style.display = 'block';
        els.historyPanel.style.display = 'block';
    }

    function showConfigPanel() {
        els.configPanel.style.display = 'block';
        els.callPanel.style.display = 'none';
        els.historyPanel.style.display = 'none';
    }

    function showIncomingCall(number) {
        els.incomingCallPanel.style.display = 'block';
        els.incomingNumber.textContent = number || '未知号码';
        els.callStatus.textContent = '来电中';
        els.callStatus.className = 'call-status ringing';
    }

    function hideIncomingCall() {
        els.incomingCallPanel.style.display = 'none';
    }

    function showActiveCallUI() {
        els.incomingCallPanel.style.display = 'none';
        els.callControls.style.display = 'grid';
        els.keypadPanel.style.display = 'block';
        els.transferPanel.style.display = 'none';
        els.callStatus.textContent = '通话中';
        els.callStatus.className = 'call-status';
    }

    function showDialerOnly() {
        els.incomingCallPanel.style.display = 'none';
        els.callControls.style.display = 'none';
        els.keypadPanel.style.display = 'block';
        els.transferPanel.style.display = 'none';
        els.callStatus.textContent = '就绪';
        els.callStatus.className = 'call-status';
        els.callerName.textContent = '请输入号码';
        els.callerNumber.textContent = '—';
        els.callerAvatar.textContent = '📞';
    }

    function resetCallUI() {
        els.callControls.style.display = 'none';
        els.keypadPanel.style.display = 'none';
        els.transferPanel.style.display = 'none';
        hideIncomingCall();
        els.callStatus.textContent = '—';
        els.callStatus.className = 'call-status ended';
        els.dialNumber.value = '';
    }

    function register() {
        const cfg = getConfig();

        if (!cfg.server || !cfg.user || !cfg.domain) {
            alert('请填写完整的 SIP 配置信息');
            return;
        }

        saveConfig();
        setStatus('connecting');

        try {
            const socketUrl = buildWsUrl(cfg);
            const socket = new JsSIP.WebSocketInterface(socketUrl);

            state.ua = new JsSIP.UA({
                sockets: [socket],
                uri: 'sip:' + cfg.user + '@' + cfg.domain,
                password: cfg.password,
                display_name: cfg.user,
                authorization_user: cfg.user,
                register: true,
                register_expires: 300,
                connection_recovery_min_interval: 2,
                connection_recovery_max_interval: 30
            });

            registerUAEvents();
            state.ua.start();

        } catch (e) {
            console.error('UA 创建失败:', e);
            setStatus('error');
            alert('SIP 注册初始化失败: ' + e.message);
        }
    }

    function buildWsUrl(cfg) {
        var transport = cfg.transport;
        var server = cfg.server;
        var port = cfg.port;

        if (transport === 'ws') {
            return 'ws://' + server + ':' + port;
        }
        return 'wss://' + server + ':' + port;
    }

    function registerUAEvents() {
        state.ua.on('connected', () => {
            setStatus('connecting');
            console.log('SIP WebSocket 连接已建立');
        });

        state.ua.on('disconnected', () => {
            console.log('SIP WebSocket 连接已断开');
        });

        state.ua.on('registered', () => {
            setStatus('connected');
            els.btnRegister.disabled = true;
            els.btnUnregister.disabled = false;
            showCallPanel();
            showDialerOnly();
            console.log('SIP 注册成功');
        });

        state.ua.on('registrationFailed', (data) => {
            setStatus('error');
            console.error('SIP 注册失败:', data);
            alert('SIP 注册失败: ' + (data.cause || '未知错误'));
        });

        state.ua.on('unregistered', () => {
            setStatus('disconnected');
            els.btnRegister.disabled = false;
            els.btnUnregister.disabled = true;
            showConfigPanel();
            console.log('SIP 已注销');
        });

        state.ua.on('incomingcall', (data) => {
            var session = data.session || data;
            handleIncomingCall(session);
        });

        state.ua.on('newRTCSession', (data) => {
            var session = data.session || data;
            if (!state.currentSession && session.direction === 'outgoing') {
                handleOutgoingCall(session);
            }
        });
    }

    function unregister() {
        if (state.ua) {
            stopCallTimer();
            cleanupMedia();
            if (state.currentSession) {
                try { state.currentSession.terminate(); } catch (e) { /* ignore */ }
                state.currentSession = null;
            }
            state.ua.stop();
            state.ua = null;
        }
    }

    function handleIncomingCall(session) {
        state.currentSession = session;

        const remoteNumber = getRemoteNumber(session);
        const remoteName = getRemoteName(session);

        els.callerName.textContent = remoteName || remoteNumber || '未知来电';
        els.callerNumber.textContent = remoteNumber || '—';
        els.callerAvatar.textContent = (remoteName || remoteNumber || '?').charAt(0).toUpperCase();
        showCallPanel();
        showIncomingCall(remoteNumber);

        session.on('accepted', () => {
            hideIncomingCall();
            showActiveCallUI();
            startCallTimer();
            setupMedia(session);
            addHistory('incoming', remoteNumber, remoteName);
        });

        session.on('rejected', () => {
            resetCallAfterEnd();
            addHistory('missed', remoteNumber, remoteName);
        });

        session.on('terminated', () => {
            const duration = stopCallTimer();
            resetCallAfterEnd();
            if (duration > 0) {
                updateHistoryDuration(remoteNumber, duration);
            }
        });

        session.on('failed', () => {
            resetCallAfterEnd();
        });

        session.on('dtmf', (data) => {
            console.log('DTMF 收到:', data.digit);
        });

        session.on('hold', () => console.log('通话保持中'));
        session.on('unhold', () => console.log('通话已恢复'));
    }

    function answerIncoming() {
        if (state.currentSession) {
            state.currentSession.answer({
                mediaConstraints: { audio: true, video: false }
            });
        }
    }

    function rejectIncoming() {
        if (state.currentSession) {
            state.currentSession.reject();
        }
    }

    function handleOutgoingCall(session) {
        state.currentSession = session;

        var number = els.dialNumber.value.trim() ||
                     extractNumberFromUri(session.request_uri) ||
                     extractNumberFromUri(session.remote_uri) ||
                     extractNumberFromUri(session.to_uri) ||
                     extractNumberFromUri(session.from_uri) ||
                     '未知';

        els.callerName.textContent = number;
        els.callerNumber.textContent = number;
        els.callerAvatar.textContent = (number || '?').charAt(0).toUpperCase();
        showCallPanel();
        els.callStatus.textContent = '呼叫中...';
        els.callStatus.className = 'call-status ringing';
        els.dialNumber.value = number;

        session.on('progress', () => {
            els.callStatus.textContent = '呼叫中...';
        });

        session.on('confirmed', () => {
            showActiveCallUI();
            startCallTimer();
            setupMedia(session);
            addHistory('outgoing', number, number);
        });

        session.on('ended', () => {
            const duration = stopCallTimer();
            resetCallAfterEnd();
            if (duration > 0) {
                updateHistoryDuration(number, duration);
            }
        });

        session.on('failed', () => {
            resetCallAfterEnd();
        });

        session.on('dtmf', (data) => {
            console.log('DTMF 发送:', data.digit);
        });

        session.on('hold', () => console.log('通话保持中'));
        session.on('unhold', () => console.log('通话已恢复'));
    }

    function setupMedia(session) {
        try {
            var connection = session.connection;
            if (!connection) return;

            var handleAddStream = function(e) {
                var stream = (e && e.streams && e.streams[0]) || (e && e.stream);
                if (stream && stream.getAudioTracks().length > 0) {
                    var outputStream = state.processingEnabled
                        ? processRemoteStream(stream)
                        : stream;
                    state.remoteAudio.srcObject = outputStream;
                    state.remoteAudio.play().catch(function() {});
                }
            };

            if (connection.addEventListener) {
                connection.addEventListener('addstream', handleAddStream);
                connection.addEventListener('removestream', function() {
                    state.remoteAudio.srcObject = null;
                });
            } else {
                connection.onaddstream = handleAddStream;
                connection.onremovestream = function() {
                    state.remoteAudio.srcObject = null;
                };
            }

            var remoteStreams = connection.getRemoteStreams
                ? connection.getRemoteStreams()
                : (connection.remoteStreams || []);
            if (remoteStreams && remoteStreams.length > 0) {
                var outputStream = state.processingEnabled
                    ? processRemoteStream(remoteStreams[0])
                    : remoteStreams[0];
                state.remoteAudio.srcObject = outputStream;
                state.remoteAudio.play().catch(function() {});
            }

            var localStreams = connection.getLocalStreams
                ? connection.getLocalStreams()
                : (connection.localStreams || []);
            if (localStreams && localStreams.length > 0) {
                state.localStream = localStreams[0];

                if (state.processingEnabled) {
                    processLocalStream(localStreams[0], connection);
                }

                var audioTracks = state.processedLocalStream
                    ? state.processedLocalStream.getAudioTracks()
                    : localStreams[0].getAudioTracks();
                audioTracks.forEach(function(track) {
                    track.enabled = !state.isMuted;
                });
            }
        } catch (e) {
            console.warn('媒体设置失败:', e);
        }
    }

    function cleanupMedia() {
        if (state.remoteAudio) {
            state.remoteAudio.srcObject = null;
        }
        state.localStream = null;
        state.originalLocalTrack = null;
        if (state.processedLocalStream) {
            state.processedLocalStream.getTracks().forEach(function(t) { t.stop(); });
            state.processedLocalStream = null;
        }
        if (state.processedRemoteStream) {
            state.processedRemoteStream.getTracks().forEach(function(t) { t.stop(); });
            state.processedRemoteStream = null;
        }
    }

    /**
     * 处理本地麦克风媒体流（出站方向）
     * 将中文语音翻译成英文后发送到远端
     *
     * 当前为占位实现：直接透传原始流，后续可替换为实际翻译管线
     * 例如：MediaStreamTrackProcessor + Web Workers + 翻译 API
     */
    function processLocalStream(localStream, connection) {
        if (!localStream || !connection) return localStream;

        var audioTracks = localStream.getAudioTracks();
        if (audioTracks.length === 0) return localStream;

        state.originalLocalTrack = audioTracks[0];

        // TODO: 实际翻译管线 —— 将中文音频转为英文
        //
        // 方案示例：
        // 1. 使用 MediaStreamTrackProcessor + ReadableStream 读取 PCM 数据
        // 2. 将音频片段发送到翻译服务（如 Web Speech API / 云端 ASR+翻译+TTS）
        // 3. 将翻译后的音频通过 MediaStreamTrackGenerator 写回
        //
        var processedTrack = audioTracks[0];

        // 创建包含处理后 track 的新 MediaStream
        var processedStream = new MediaStream();
        processedStream.addTrack(processedTrack.clone());

        state.processedLocalStream = processedStream;

        // 替换 RTCPeerConnection 中的发送 track
        try {
            var senders = connection.getSenders();
            var audioSender = null;
            for (var i = 0; i < senders.length; i++) {
                if (senders[i].track && senders[i].track.kind === 'audio') {
                    audioSender = senders[i];
                    break;
                }
            }
            if (audioSender) {
                audioSender.replaceTrack(processedTrack.clone());
                console.log('[媒体处理] 已替换发送端 track，出站翻译管线已接入');
            } else {
                connection.addTrack(processedTrack.clone(), processedStream);
                console.log('[媒体处理] 已添加处理后 track，出站翻译管线已接入');
            }
        } catch (e) {
            console.warn('[媒体处理] 替换发送 track 失败:', e);
        }

        return processedStream;
    }

    /**
     * 处理远端媒体流（入站方向）
     * 将远端英文语音翻译成中文后播放到扬声器
     *
     * 当前为占位实现：直接透传原始流，后续可替换为实际翻译管线
     */
    function processRemoteStream(remoteStream) {
        if (!remoteStream) return remoteStream;

        var audioTracks = remoteStream.getAudioTracks();
        if (audioTracks.length === 0) return remoteStream;

        // TODO: 实际翻译管线 —— 将英文音频转为中文
        //
        // 方案示例：
        // 1. 使用 MediaStreamTrackProcessor + ReadableStream 读取 PCM 数据
        // 2. 将音频片段发送到翻译服务（如 Web Speech API / 云端 ASR+翻译+TTS）
        // 3. 将翻译后的音频通过 MediaStreamTrackGenerator 写回
        //
        var processedTrack = audioTracks[0];

        var processedStream = new MediaStream();
        processedStream.addTrack(processedTrack.clone());

        state.processedRemoteStream = processedStream;

        console.log('[媒体处理] 远端媒体已接入入站翻译管线（占位透传）');
        return processedStream;
    }

    function makeOutgoingCall(number) {
        if (!state.ua) {
            alert('请先注册 SIP 账户');
            return;
        }
        if (!number) {
            alert('请输入电话号码');
            return;
        }

        const cfg = getConfig();
        const targetUri = 'sip:' + number + '@' + cfg.domain;

        try {
            state.ua.call(targetUri, {
                mediaConstraints: { audio: true, video: false }
            });
        } catch (e) {
            console.error('拨号失败:', e);
            alert('拨号失败: ' + e.message);
        }
    }

    function dial() {
        const number = els.dialNumber.value.trim();
        if (number) {
            makeOutgoingCall(number);
        }
    }

    function hangup() {
        if (state.currentSession) {
            try { state.currentSession.terminate(); } catch (e) { /* ignore */ }
            state.currentSession = null;
        }
        state.isMuted = false;
        state.isSpeakerOn = false;
        els.btnMute.classList.remove('active');
        els.btnSpeaker.classList.remove('active');
        cleanupMedia();
        stopCallTimer();
        resetCallUI();
        showDialerOnly();
    }

    function resetCallAfterEnd() {
        state.currentSession = null;
        state.isMuted = false;
        state.isSpeakerOn = false;
        els.btnMute.classList.remove('active');
        els.btnSpeaker.classList.remove('active');
        cleanupMedia();
        stopCallTimer();
        resetCallUI();
        showDialerOnly();
    }

    function sendDTMF(digit) {
        if (state.currentSession && state.currentSession.status === STATUS_CONFIRMED) {
            try {
                state.currentSession.dtmf(digit);
            } catch (e) {
                console.warn('DTMF 发送失败:', e);
            }
        }
    }

    function addDigit(digit) {
        els.dialNumber.value += digit;
    }

    function clearDialInput() {
        els.dialNumber.value = els.dialNumber.value.slice(0, -1);
    }

    function toggleMute() {
        state.isMuted = !state.isMuted;
        els.btnMute.classList.toggle('active', state.isMuted);

        var stream = state.processedLocalStream || state.localStream;
        if (stream) {
            stream.getAudioTracks().forEach(function(track) {
                track.enabled = !state.isMuted;
            });
        }
    }

    function toggleSpeaker() {
        state.isSpeakerOn = !state.isSpeakerOn;
        els.btnSpeaker.classList.toggle('active', state.isSpeakerOn);

        if (state.remoteAudio) {
            state.remoteAudio.muted = state.isSpeakerOn;
        }
    }

    function toggleKeypad() {
        const isVisible = els.keypadPanel.style.display !== 'none';
        els.keypadPanel.style.display = isVisible ? 'none' : 'block';
    }

    function toggleTransfer() {
        const isVisible = els.transferPanel.style.display !== 'none';
        els.transferPanel.style.display = isVisible ? 'none' : 'block';
        if (!isVisible) {
            els.transferNumber.focus();
        }
    }

    function confirmTransfer() {
        const number = els.transferNumber.value.trim();
        if (!state.currentSession || !number) {
            if (!number) alert('请输入转接号码');
            return;
        }

        try {
            const cfg = getConfig();
            const transferTarget = 'sip:' + number + '@' + cfg.domain;

            state.currentSession.refer(transferTarget);

            els.transferPanel.style.display = 'none';
            addHistory('outgoing', number, '转接至');

        } catch (e) {
            console.error('转接失败:', e);
            alert('转接失败: ' + e.message);
        }
    }

    function cancelTransfer() {
        els.transferPanel.style.display = 'none';
        els.transferNumber.value = '';
    }

    function startCallTimer() {
        state.callStartTime = Date.now();
        updateCallTimer();
        state.callTimerInterval = setInterval(updateCallTimer, 1000);
    }

    function stopCallTimer() {
        let duration = 0;
        if (state.callStartTime) {
            duration = Math.floor((Date.now() - state.callStartTime) / 1000);
        }
        if (state.callTimerInterval) {
            clearInterval(state.callTimerInterval);
            state.callTimerInterval = null;
        }
        state.callStartTime = null;
        els.callTimer.textContent = '00:00';
        return duration;
    }

    function updateCallTimer() {
        if (!state.callStartTime) return;
        const elapsed = Math.floor((Date.now() - state.callStartTime) / 1000);
        const mins = String(Math.floor(elapsed / 60)).padStart(2, '0');
        const secs = String(elapsed % 60).padStart(2, '0');
        els.callTimer.textContent = mins + ':' + secs;
    }

    function getRemoteNumber(session) {
        try {
            if (session.remote_identity) {
                if (session.remote_identity.uri) {
                    var u = session.remote_identity.uri;
                    if (u.user) return u.user;
                    var n = extractNumberFromUri(u);
                    if (n) return n;
                }
                if (Array.isArray(session.remote_identity) && session.remote_identity[0]) {
                    var first = session.remote_identity[0];
                    if (first.uri && first.uri.user) return first.uri.user;
                }
            }
            if (session.from_uri) {
                var n = extractNumberFromUri(session.from_uri);
                if (n) return n;
            }
            if (session.remote_uri) {
                var n = extractNumberFromUri(session.remote_uri);
                if (n) return n;
            }
            if (session.request_uri) {
                var n = extractNumberFromUri(session.request_uri);
                if (n) return n;
            }
            if (session.to_uri) {
                var n = extractNumberFromUri(session.to_uri);
                if (n) return n;
            }
        } catch (e) { /* ignore */ }
        return '未知';
    }

    function getRemoteName(session) {
        try {
            if (session.remote_identity) {
                if (session.remote_identity.display_name) {
                    return session.remote_identity.display_name;
                }
                if (session.remote_identity.uri && session.remote_identity.uri.display_name) {
                    return session.remote_identity.uri.display_name;
                }
            }
        } catch (e) { /* ignore */ }
        return null;
    }

    function extractNumberFromUri(uri) {
        if (!uri) return '';
        var str;
        if (typeof uri === 'string') {
            str = uri;
        } else if (uri instanceof Object) {
            if (uri.user) return uri.user;
            if (uri.host) str = uri.scheme + ':' + uri.user + '@' + uri.host;
            else str = String(uri);
        } else {
            str = String(uri);
        }
        var match = str.match(/sips?:([^@]+)/);
        if (match) return match[1];
        match = str.match(/^([^@]+)/);
        if (match && match[1] && !match[1].match(/^wss?:/)) return match[1];
        return '';
    }

    function loadHistory() {
        try {
            const saved = localStorage.getItem('softphone_history');
            if (saved) {
                state.history = JSON.parse(saved);
            }
        } catch (e) { state.history = []; }
    }

    function saveHistory() {
        localStorage.setItem('softphone_history', JSON.stringify(state.history));
    }

    function addHistory(type, number, name) {
        var item = {
            id: Date.now(),
            type: type,
            number: number || '未知',
            name: name || number || '未知',
            time: new Date().toLocaleString('zh-CN', {
                month: '2-digit', day: '2-digit',
                hour: '2-digit', minute: '2-digit'
            }),
            duration: 0
        };
        state.history.unshift(item);
        if (state.history.length > 50) {
            state.history = state.history.slice(0, 50);
        }
        saveHistory();
        renderHistory();
    }

    function updateHistoryDuration(number, duration) {
        if (state.history.length > 0) {
            state.history[0].duration = duration;
            saveHistory();
            renderHistory();
        }
    }

    function clearHistory() {
        if (confirm('确定要清空通话记录吗？')) {
            state.history = [];
            saveHistory();
            renderHistory();
        }
    }

    function renderHistory() {
        if (state.history.length === 0) {
            els.historyList.innerHTML = '<div style="text-align:center;color:#999;padding:20px;">暂无通话记录</div>';
            return;
        }

        var iconMap = { incoming: '📥', outgoing: '📤', missed: '❌' };

        els.historyList.innerHTML = state.history.map(function (item) {
            var durationText = item.duration > 0 ? formatDuration(item.duration) : '';
            return '' +
                '<div class="history-item">' +
                    '<div class="history-icon ' + item.type + '">' +
                        (iconMap[item.type] || '📞') +
                    '</div>' +
                    '<div class="history-info">' +
                        '<div class="history-number">' + escapeHtml(item.number) + '</div>' +
                        '<div class="history-time">' + item.time + '</div>' +
                    '</div>' +
                    '<div class="history-duration">' + durationText + '</div>' +
                '</div>';
        }).join('');
    }

    function formatDuration(seconds) {
        var mins = Math.floor(seconds / 60);
        var secs = seconds % 60;
        if (mins > 0) return mins + '分' + secs + '秒';
        return secs + '秒';
    }

    function escapeHtml(str) {
        var div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    window.addEventListener('error', function (e) {
        console.error('全局错误:', e.message);
    });

    window.__softphone = { state: state, getConfig: getConfig };

})();