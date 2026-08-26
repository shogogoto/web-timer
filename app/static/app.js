let reminderAudioContext = null;

async function unlockReminderAudio() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) return null;
  if (!reminderAudioContext) reminderAudioContext = new AudioContextClass();
  if (reminderAudioContext.state === 'suspended') await reminderAudioContext.resume();
  return reminderAudioContext;
}

async function playReminderSound() {
  const context = await unlockReminderAudio();
  if (!context || context.state !== 'running') return false;
  const startedAt = context.currentTime;
  const master = context.createGain();
  master.gain.setValueAtTime(.001, startedAt);
  master.gain.exponentialRampToValueAtTime(.55, startedAt + .01);
  master.gain.exponentialRampToValueAtTime(.001, startedAt + 1.8);
  master.connect(context.destination);
  [[1046.5, 0], [1568, .14], [2093, .28]].forEach(([frequency, delay]) => {
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = 'sine';
    oscillator.frequency.setValueAtTime(frequency, startedAt + delay);
    gain.gain.setValueAtTime(.001, startedAt + delay);
    gain.gain.exponentialRampToValueAtTime(.5, startedAt + delay + .01);
    gain.gain.exponentialRampToValueAtTime(.001, startedAt + delay + 1.15);
    oscillator.connect(gain).connect(master);
    oscillator.start(startedAt + delay);
    oscillator.stop(startedAt + delay + 1.2);
  });
  return true;
}

document.addEventListener('pointerdown', () => { unlockReminderAudio().catch(() => {}); }, {once: true, capture: true});
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.addEventListener('message', event => {
    if (event.data?.type === 'reminder') playReminderSound().catch(() => {});
  });
}

function studyTimer(initial, defaultSeconds, activityDetails, todayDate) {
  return {
    selectedSeconds: initial ? initial.planned : defaultSeconds,
    remaining: initial ? initial.remaining : defaultSeconds,
    phase: initial ? initial.status : 'select',
    sessionId: initial ? initial.id : null,
    error: '', copyStatus: '', interval: null, endAt: null, audioContext: null, pushRegistration: null, hasRung: false, hotkeyPending: false,
    activityDetails, selectedDate: todayDate,
    get display() { const s = this.phase === 'select' ? this.selectedSeconds : this.remaining; return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}` },
    get selectedActivity() { return this.activityDetails[this.selectedDate] || {seconds:0,completed:0,stopped:0,sessions:[],hourly:[],ticks:[]} },
    get selectedDateLabel() { const [year,month,day]=this.selectedDate.split('-').map(Number); return `${month}月${day}日` },
    formatDuration(seconds) { if(seconds<60)return `${seconds}秒`; const minutes=Math.floor(seconds/60), rest=seconds%60; return rest ? `${minutes}分${rest}秒` : `${minutes}分` },
    async copyReport(text) {
      try {
        if (!navigator.clipboard?.writeText) throw new Error('Clipboard API unavailable');
        await navigator.clipboard.writeText(text);
        this.copyStatus='コピーしました'; setTimeout(()=>{this.copyStatus=''},2500);
      } catch (_) {
        const area=document.createElement('textarea'); area.value=text; area.setAttribute('readonly','');
        area.style.position='fixed'; area.style.left='-9999px'; document.body.appendChild(area);
        area.focus(); area.select();
        const copied=document.execCommand('copy'); area.remove();
        this.copyStatus=copied ? 'コピーしました' : 'コピーできませんでした';
        if(copied)setTimeout(()=>{this.copyStatus=''},2500);
      }
    },
    init() { if('serviceWorker' in navigator) navigator.serviceWorker.addEventListener('message',event=>{if(event.data?.type==='timer-finished'){this.phase='finished'; this.remaining=0; this.ring();}}); this.registerPushWorker(); if (this.phase === 'running') this.runClock(); },
    async handleTimerHotkey(event) {
      if (event.repeat || event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) return;
      if (event.key !== ' ' && event.key !== 'Enter') return;
      const target = event.target;
      if (target instanceof Element && (target.isContentEditable || target.closest('input, textarea, select, button, a'))) return;
      if (!['select', 'ready', 'running', 'paused'].includes(this.phase)) return;
      event.preventDefault();
      if (this.hotkeyPending) return;
      this.hotkeyPending = true;
      try {
        if (this.phase === 'select') await this.setTimer();
        else if (this.phase === 'ready') await this.start();
        else if (this.phase === 'running') await this.pause();
        else if (this.phase === 'paused') await this.resume();
      } finally {
        this.hotkeyPending = false;
      }
    },
    async request(path, body) {
      const options = {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}};
      if (body) options.body = new URLSearchParams(body);
      const response = await fetch(path, options);
      if (!response.ok) throw new Error((await response.json()).detail || '操作に失敗しました');
      return response.json();
    },
    unlockAudio() { if (!this.audioContext) this.audioContext = new (window.AudioContext || window.webkitAudioContext)(); if(this.audioContext.state==='suspended')this.audioContext.resume(); },
    async setDebugTimer(seconds) { try { this.unlockAudio(); await this.askNotification(); const r=await this.request('/api/sessions',{planned_seconds:seconds}); this.sessionId=r.id; this.remaining=r.remaining; this.phase=r.status; this.runClock(); } catch(e){this.error=e.message} },
    async setTimer() { try { this.unlockAudio(); await this.askNotification(); const r=await this.request('/api/sessions',{planned_seconds:this.selectedSeconds}); this.sessionId=r.id; this.remaining=r.remaining; this.phase=r.status; this.runClock(); } catch(e){this.error=e.message} },
    async start() { try { this.unlockAudio(); const r=await this.request(`/api/sessions/${this.sessionId}/start`); this.remaining=r.remaining; this.phase='running'; this.runClock(); } catch(e){this.error=e.message} },
    runClock() { clearInterval(this.interval); this.endAt=Date.now()+this.remaining*1000; this.interval=setInterval(()=>{this.remaining=Math.max(0,Math.ceil((this.endAt-Date.now())/1000)); if(this.remaining===0)this.finish(true)},250); },
    async pause() { try { const r=await this.request(`/api/sessions/${this.sessionId}/pause`); clearInterval(this.interval); this.remaining=r.remaining; this.phase='paused'; } catch(e){this.error=e.message} },
    async resume() { try { const r=await this.request(`/api/sessions/${this.sessionId}/resume`); this.remaining=r.remaining; this.phase='running'; this.runClock(); } catch(e){this.error=e.message} },
    async finish(completed) { try { clearInterval(this.interval); if(completed){this.phase='finished'; this.ring();} await this.request(`/api/sessions/${this.sessionId}/finish`); if(completed) setTimeout(()=>location.reload(),2800); else location.reload(); } catch(e){this.error=e.message} },
    async registerPushWorker() { if(!window.isSecureContext){this.error='バックグラウンド通知を使うにはHTTPSでアクセスしてください'; return;} if('serviceWorker' in navigator && 'PushManager' in window) this.pushRegistration=await navigator.serviceWorker.register('/sw.js'); },
    async askNotification() { try { if(!window.isSecureContext || !('Notification' in window) || !('serviceWorker' in navigator)) return; if(Notification.permission==='default') await Notification.requestPermission(); if(Notification.permission!=='granted') return; const registration=this.pushRegistration || await navigator.serviceWorker.ready, config=await fetch('/api/push/config').then(r=>r.json()), existing=await registration.pushManager.getSubscription(), subscription=existing || await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:this.urlBase64ToUint8Array(config.application_server_key)}); await fetch('/api/push/subscriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(subscription)}); } catch(e){console.warn('Push notification setup failed',e)} },
    urlBase64ToUint8Array(value) { const padding='='.repeat((4-value.length%4)%4), base64=(value+padding).replace(/-/g,'+').replace(/_/g,'/'), raw=atob(base64); return Uint8Array.from([...raw].map(c=>c.charCodeAt(0))); },
    ring() { if(this.hasRung)return; this.hasRung=true; if(this.audioContext){this.audioContext.resume(); const at=this.audioContext.currentTime, master=this.audioContext.createGain(); master.gain.setValueAtTime(.75,at); master.gain.exponentialRampToValueAtTime(.001,at+2.4); master.connect(this.audioContext.destination); [[880,.65,2.4],[2425,.22,1.5],[4755,.1,.9],[7861,.045,.55]].forEach(([frequency,volume,decay])=>{const oscillator=this.audioContext.createOscillator(), gain=this.audioContext.createGain(); oscillator.type='sine'; oscillator.frequency.setValueAtTime(frequency,at); oscillator.frequency.exponentialRampToValueAtTime(frequency*.997,at+decay); gain.gain.setValueAtTime(.001,at); gain.gain.exponentialRampToValueAtTime(volume,at+.006); gain.gain.exponentialRampToValueAtTime(.001,at+decay); oscillator.connect(gain).connect(master); oscillator.start(at); oscillator.stop(at+decay+.05);});} }
  }
}

function activityExplorer(activityDetails, selectedDate) {
  return {
    activityDetails,
    selectedDate,
    get selectedActivity() {
      return this.activityDetails[this.selectedDate] || {seconds:0,completed:0,stopped:0,sessions:[],hourly:[],ticks:[]};
    },
    get selectedDateLabel() {
      const [year, month, day] = this.selectedDate.split('-').map(Number);
      return `${month}月${day}日`;
    },
    formatDuration(seconds) {
      if (seconds < 60) return `${seconds}秒`;
      const minutes = Math.floor(seconds / 60), rest = seconds % 60;
      return rest ? `${minutes}分${rest}秒` : `${minutes}分`;
    }
  }
}

function reminderForm() {
  return {
    error: '',
    submitting: false,
    async testSound() {
      this.error = '';
      if (!await playReminderSound()) this.error = 'このブラウザでは通知音を再生できません';
    },
    async submit(event) {
      if (this.submitting) return;
      try {
        await unlockReminderAudio();
        if (!window.isSecureContext || !('Notification' in window) || !('serviceWorker' in navigator) || !('PushManager' in window)) {
          throw new Error('リマインダー通知を使うにはHTTPS対応ブラウザで開いてください');
        }
        if (Notification.permission === 'default') await Notification.requestPermission();
        if (Notification.permission !== 'granted') throw new Error('ブラウザの通知を許可してください');
        const registration = await navigator.serviceWorker.register('/sw.js');
        const config = await fetch('/api/push/config').then(response => response.json());
        const existing = await registration.pushManager.getSubscription();
        const subscription = existing || await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: this.urlBase64ToUint8Array(config.application_server_key)
        });
        const response = await fetch('/api/push/subscriptions', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(subscription)
        });
        if (!response.ok) throw new Error('通知の登録に失敗しました');
        this.submitting = true;
        event.target.submit();
      } catch (error) {
        this.error = error.message || '通知の登録に失敗しました';
      }
    },
    urlBase64ToUint8Array(value) {
      const padding = '='.repeat((4 - value.length % 4) % 4);
      const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/');
      const raw = atob(base64);
      return Uint8Array.from([...raw].map(character => character.charCodeAt(0)));
    }
  }
}
