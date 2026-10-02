// 자동 생성 파일 — 직접 고치지 말 것.  원본: config/wehago/guard.yaml
// 재생성: python -m taxauto.wehago.guard --write-init-script
// 브라우저 안에서 금지 문구 버튼/링크의 클릭·제출을 막는다(DOM 기반 화면에서만 유효, 캔버스 그리드는 못 막음).
(() => {
  if (window.__taxautoGuardInstalled) return;
  window.__taxautoGuardInstalled = true;
  const CFG = {"terms": ["제출", "신고하기", "신고서전송", "국세청전송", "전송", "발행", "발급", "신고취소", "접수", "일괄삭제", "전체삭제", "회사삭제", "거래처삭제", "데이터삭제", "초기화", "마감취소", "마감해제", "마감", "비밀번호", "인증서", "회원탈퇴", "탈퇴", "해지", "권한설정", "권한변경", "사용자관리", "직원초대", "결제", "구매", "충전", "납부하기", "전자납부", "카드납부", "이체", "발송", "문자보내기", "메일보내기", "submit", "password", "deleteall"], "exceptions": [], "blockedHosts": ["hometax.go.kr", "*.hometax.go.kr", "nts.go.kr", "*.nts.go.kr", "*.wetax.go.kr", "wetax.go.kr"]};
  const norm = (s) => String(s || '').normalize('NFKC').replace(/[\s​-‍﻿]+/g, '').toLowerCase();
  const excused = (t, i, n) => CFG.exceptions.some((ex) => {
    let j = t.indexOf(ex);
    while (j !== -1) { if (j <= i && i + n <= j + ex.length) return true; j = t.indexOf(ex, j + 1); }
    return false;
  });
  const forbidden = (text) => {
    const t = norm(text);
    if (!t) return null;
    for (const term of CFG.terms) {
      let i = t.indexOf(term);
      while (i !== -1) { if (!excused(t, i, term.length)) return term; i = t.indexOf(term, i + 1); }
    }
    return null;
  };
  const INTERACTIVE = 'button,a,[role=button],[role=menuitem],[role=tab],[role=link],[role=option],input[type=button],input[type=submit],input[type=image],[onclick]';
  const labelOf = (el) => {
    if (!el || el.nodeType !== 1) return '';
    const v = (el.tagName === 'INPUT' && /^(button|submit|image)$/i.test(el.type || '')) ? el.value : '';
    return [el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('alt'), v, el.innerText || el.textContent]
      .filter(Boolean).join(' ').slice(0, 300);
  };
  const targetText = (ev) => {
    const path = ev.composedPath ? ev.composedPath() : [ev.target];
    for (const n of path) {
      if (n && n.nodeType === 1 && n.matches && n.matches(INTERACTIVE)) {
        const s = labelOf(n);
        if (s.length <= 120) return s;   // 큰 컨테이너는 오탐 방지 위해 제외
        break;
      }
    }
    const s = labelOf(ev.target);
    return s.length <= 60 ? s : '';
  };
  const blockedHost = (href) => {
    try {
      const h = new URL(href, location.href).hostname.toLowerCase();
      return CFG.blockedHosts.some((b) => b.startsWith('*.') ? (h === b.slice(2) || h.endsWith(b.slice(1))) : (h === b || h.endsWith('.' + b)));
    } catch (e) { return false; }
  };
  const record = (kind, why, text) => {
    const r = { kind, why, text: String(text || '').slice(0, 80), at: new Date().toISOString() };
    (window.__taxautoGuardBlocked = window.__taxautoGuardBlocked || []).push(r);
    try { console.warn('[taxauto-guard] 차단 ' + JSON.stringify(r)); } catch (e) {}
  };
  const stop = (ev, why, text) => { ev.preventDefault(); ev.stopImmediatePropagation(); ev.stopPropagation(); record(ev.type, why, text); };
  const onPointer = (ev) => {
    const text = targetText(ev);
    const hit = forbidden(text);
    if (hit) return stop(ev, '금지어:' + hit, text);
    const a = ev.target && ev.target.closest && ev.target.closest('a[href]');
    if (a && blockedHost(a.getAttribute('href'))) return stop(ev, '차단도메인', a.getAttribute('href'));
  };
  for (const t of ['click', 'dblclick', 'auxclick', 'mousedown', 'mouseup', 'pointerdown', 'pointerup']) {
    window.addEventListener(t, onPointer, true);
  }
  window.addEventListener('submit', (ev) => {
    const s = ev.submitter ? labelOf(ev.submitter) : '';
    const hit = forbidden(s) || forbidden(ev.target && ev.target.getAttribute && (ev.target.getAttribute('action') || ''));
    if (hit) stop(ev, '금지어:' + hit, s);
  }, true);
  const _open = window.open;
  window.open = function (url, ...rest) {
    if (url && blockedHost(String(url))) { record('window.open', '차단도메인', String(url)); return null; }
    return _open.call(this, url, ...rest);
  };
})();
