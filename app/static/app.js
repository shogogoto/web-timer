function studyTimer(initial) {
  return {
    minutes: initial ? initial.planned / 60 : 40,
    remaining: initial ? initial.remaining : 2400,
    phase: initial ? initial.status : 'select',
    sessionId: initial ? initial.id : null,
    error: '', interval: null, endAt: null, audioContext: null, pushRegistration: null, hasRung: false,
    get display() { const s = this.phase === 'select' ? this.minutes * 60 : this.remaining; return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}` },
    init() { window.addEventListener('pagehide',()=>this.pauseOnPageExit()); if('serviceWorker' in navigator) navigator.serviceWorker.addEventListener('message',event=>{if(event.data?.type==='timer-finished'){this.phase='finished'; this.remaining=0; this.ring();}}); this.registerPushWorker(); if (this.phase === 'running') this.runClock(); },
    async request(path, body) {
      const options = {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}};
      if (body) options.body = new URLSearchParams(body);
      const response = await fetch(path, options);
      if (!response.ok) throw new Error((await response.json()).detail || '操作に失敗しました');
      return response.json();
    },
    unlockAudio() { if (!this.audioContext) this.audioContext = new (window.AudioContext || window.webkitAudioContext)(); if(this.audioContext.state==='suspended')this.audioContext.resume(); },
    async setDebugTimer(seconds) { try { this.unlockAudio(); const r=await this.request('/api/sessions',{planned_seconds:seconds}); this.sessionId=r.id; this.remaining=r.remaining; this.phase='ready'; this.askNotification(); } catch(e){this.error=e.message} },
    async setTimer() { try { this.unlockAudio(); const r=await this.request('/api/sessions',{planned_seconds:this.minutes*60}); this.sessionId=r.id; this.remaining=r.remaining; this.phase='ready'; this.askNotification(); } catch(e){this.error=e.message} },
    async start() { try { this.unlockAudio(); const r=await this.request(`/api/sessions/${this.sessionId}/start`); this.remaining=r.remaining; this.phase='running'; this.runClock(); } catch(e){this.error=e.message} },
    runClock() { clearInterval(this.interval); this.endAt=Date.now()+this.remaining*1000; this.interval=setInterval(()=>{this.remaining=Math.max(0,Math.ceil((this.endAt-Date.now())/1000)); if(this.remaining===0)this.finish(true)},250); },
    async pause() { try { const r=await this.request(`/api/sessions/${this.sessionId}/pause`); clearInterval(this.interval); this.remaining=r.remaining; this.phase='paused'; } catch(e){this.error=e.message} },
    pauseOnPageExit() { if(this.phase==='running' && this.sessionId) navigator.sendBeacon(`/api/sessions/${this.sessionId}/pause`); },
    async resume() { try { const r=await this.request(`/api/sessions/${this.sessionId}/resume`); this.remaining=r.remaining; this.phase='running'; this.runClock(); } catch(e){this.error=e.message} },
    async finish(completed) { try { clearInterval(this.interval); if(completed){this.phase='finished'; this.ring();} await this.request(`/api/sessions/${this.sessionId}/finish`); if(completed) setTimeout(()=>location.reload(),2800); else location.reload(); } catch(e){this.error=e.message} },
    async registerPushWorker() { if(!window.isSecureContext){this.error='バックグラウンド通知を使うにはHTTPSでアクセスしてください'; return;} if('serviceWorker' in navigator && 'PushManager' in window) this.pushRegistration=await navigator.serviceWorker.register('/sw.js'); },
    async askNotification() { try { if(!window.isSecureContext || !('Notification' in window) || !('serviceWorker' in navigator)) return; if(Notification.permission==='default') await Notification.requestPermission(); if(Notification.permission!=='granted') return; const registration=this.pushRegistration || await navigator.serviceWorker.ready, config=await fetch('/api/push/config').then(r=>r.json()), existing=await registration.pushManager.getSubscription(), subscription=existing || await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:this.urlBase64ToUint8Array(config.application_server_key)}); await fetch('/api/push/subscriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(subscription)}); } catch(e){console.warn('Push notification setup failed',e)} },
    urlBase64ToUint8Array(value) { const padding='='.repeat((4-value.length%4)%4), base64=(value+padding).replace(/-/g,'+').replace(/_/g,'/'), raw=atob(base64); return Uint8Array.from([...raw].map(c=>c.charCodeAt(0))); },
    ring() { if(this.hasRung)return; this.hasRung=true; if(this.audioContext){this.audioContext.resume(); const at=this.audioContext.currentTime, master=this.audioContext.createGain(); master.gain.setValueAtTime(.75,at); master.gain.exponentialRampToValueAtTime(.001,at+2.4); master.connect(this.audioContext.destination); [[880,.65,2.4],[2425,.22,1.5],[4755,.1,.9],[7861,.045,.55]].forEach(([frequency,volume,decay])=>{const oscillator=this.audioContext.createOscillator(), gain=this.audioContext.createGain(); oscillator.type='sine'; oscillator.frequency.setValueAtTime(frequency,at); oscillator.frequency.exponentialRampToValueAtTime(frequency*.997,at+decay); gain.gain.setValueAtTime(.001,at); gain.gain.exponentialRampToValueAtTime(volume,at+.006); gain.gain.exponentialRampToValueAtTime(.001,at+decay); oscillator.connect(gain).connect(master); oscillator.start(at); oscillator.stop(at+decay+.05);});} }
  }
}
