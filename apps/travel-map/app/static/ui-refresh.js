/** Seoul education travel map: presentation only. Load BEFORE app.js.
 * No requests, storage, calculation, new map SDK, or auth changes.
 * Original input DOM objects are moved rather than replaced.
 */
(() => {
  'use strict';
  const $ = (s, root = document) => root.querySelector(s);
  const all = (s, root = document) => [...root.querySelectorAll(s)];
  const required = ['.app-shell','.rail','.input-section','#trip-form','#results',
    '.trip-patterns','.schedule-fields','.date-time-grid','.duration-control',
    '.vehicle-assumptions','.map-stage','#calculate-button','#retention-notice'];
  if (document.body.dataset.travelUi === '2' || required.some(s => !$(s))) return;
  const make = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const icon = name => {
    const shapes = {
      route:['M5 5h8a4 4 0 0 1 0 8H9a4 4 0 0 0 0 8h10','m16 18 3 3-3 3'],
      shield:['M12 3 4 6v5c0 5 4 8 8 10 4-2 8-5 8-10V6l-8-3Z','m8 12 3 3 5-6'],
      pin:['M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 1 1 14 0Z','M12 7v6','M9 10h6'],
      chevron:['m8 10 4 4 4-4'],
    };
    const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.classList.add('ui-icon');
    for (const [k,v] of Object.entries({viewBox:name==='route'?'0 0 26 27':'0 0 24 24',
      'aria-hidden':'true',fill:'none',stroke:'currentColor','stroke-width':'1.8',
      'stroke-linecap':'round','stroke-linejoin':'round'})) svg.setAttribute(k,v);
    for (const d of shapes[name]) {
      const p = document.createElementNS(svg.namespaceURI,'path'); p.setAttribute('d',d); svg.append(p);
    }
    return svg;
  };
  const body=document.body, form=$('#trip-form'), inputPanel=$('.input-section');
  const results=$('#results'), rail=$('.rail'), calculate=$('#calculate-button');
  const observers=[];
  body.dataset.travelUi='2'; body.classList.add('travel-ui-v2','ui-form-view');
  const brand=$('.brand');
  if(brand){
    const mark=make('span','ui-brand-mark'); mark.append(icon('route'));
    const words=make('span','ui-brand-words');
    words.append(make('strong','','서울교육 출장지도'),make('small','','경로와 예상 여비를 한 번에'));
    brand.replaceChildren(mark,words);
  }
  const privacy=make('span','ui-privacy'); privacy.hidden=true;
  privacy.append(icon('shield'),make('span','','입력·결과 저장 안 함'));
  $('.app-header')?.append(privacy);
  const syncPrivacy=()=>{privacy.hidden=!/1회성 베타/.test($('#retention-notice').textContent);};
  const po=new MutationObserver(syncPrivacy);
  po.observe($('#retention-notice'),{childList:true,subtree:true,characterData:true}); observers.push(po); syncPrivacy();
  const intro=make('section','ui-intro'); intro.setAttribute('aria-labelledby','ui-page-title');
  const introCopy=make('div','ui-intro-copy');
  const title=make('h1','','출장 준비는 간단하게.'); title.id='ui-page-title';
  introCopy.append(title,make('p','','어디로, 언제 가시나요? 경로와 예상 여비를 함께 확인하세요.'));
  const progress=make('div','ui-flow-hint');
  progress.setAttribute('aria-label','이용 순서: 조건 입력, 경로 비교, 예상 여비 확인');
  ['조건 입력','경로 비교','예상 여비'].forEach((text,i)=>{
    const item=make('span',i===0?'is-active':'',text); item.prepend(make('b','',String(i+1)));
    progress.append(item); if(i<2)progress.append(icon('chevron'));
  });
  intro.append(introCopy,progress); $('.app-shell').before(intro);
  const oldHeading=$('#conditions-title');
  if(oldHeading){const h=make('h2','','출장 조건'); h.id=oldHeading.id; oldHeading.replaceWith(h);}
  if($('#results-title'))$('#results-title').textContent='경로와 예상 여비';
  $('.steps')?.setAttribute('hidden','');
  // app.js alone owns results.hidden. The UI never overwrites that source of truth.
  const tabs=make('div','ui-view-tabs');
  tabs.setAttribute('role','tablist'); tabs.setAttribute('aria-label','출장 조건과 계산 결과');
  const inputTab=make('button','ui-view-tab','출장 조건'); inputTab.id='ui-form-tab';
  const resultTab=make('button','ui-view-tab','계산 결과'); resultTab.id='ui-results-tab';
  inputPanel.id||='ui-input-panel';
  for(const [button,panel]of [[inputTab,inputPanel],[resultTab,results]]){
    button.type='button';button.setAttribute('role','tab');button.setAttribute('aria-controls',panel.id);
    panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',button.id);
  }
  resultTab.disabled=results.hidden; tabs.append(inputTab,resultTab); rail.prepend(tabs);
  let currentView='form';
  function setView(view,focus=false){
    const isResult=view==='results'&&!results.hidden;
    currentView=isResult?'results':'form';
    body.classList.toggle('ui-form-view',!isResult); body.classList.toggle('ui-results-view',isResult);
    inputPanel.hidden=isResult;
    inputTab.setAttribute('aria-selected',String(!isResult)); resultTab.setAttribute('aria-selected',String(isResult));
    inputTab.tabIndex=isResult?-1:0; resultTab.tabIndex=isResult?0:-1;
    results.setAttribute('aria-hidden',String(!isResult));
    all('.ui-flow-hint > span').forEach((n,i)=>n.classList.toggle('is-active',isResult?i>0:i===0));
    if(focus){
      const target=isResult?$('#results-title'):$('#conditions-title');
      target?.setAttribute('tabindex','-1');target?.focus({preventScroll:true});
      if(matchMedia('(max-width: 767px)').matches)rail.scrollIntoView({block:'start',behavior:'instant'});
    }
  }
  inputTab.addEventListener('click',()=>setView('form'));
  resultTab.addEventListener('click',()=>setView('results'));
  tabs.addEventListener('keydown',e=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
    e.preventDefault();const next=e.key==='Home'||e.key==='ArrowLeft'||resultTab.disabled?inputTab:resultTab;
    next.click();next.focus();
  });
  // app.js owns results.hidden, but a recalculation can render fresh results
  // while the attribute already allows display, so the rendered subtree is
  // watched as well; focus only moves when the visible view really changes.
  const ro=new MutationObserver(()=>{resultTab.disabled=results.hidden;const next=results.hidden?'form':'results';setView(next,next!==currentView&&next==='results');});
  ro.observe(results,{attributes:true,attributeFilter:['hidden'],childList:true,subtree:true});observers.push(ro);setView(results.hidden?'form':'results');
  const patterns=$('.trip-patterns');form.prepend(patterns);
  const copy={
    ROUND_TRIP:['왕복','가는 길 + 복귀','일반 왕복'],
    OUTBOUND_ONLY_END_AFTER_SCHEDULE:['일정 후 퇴근','가는 길만','일정 후 퇴근'],
    RETURN_ONLY_DIRECT_TO_DESTINATION:['바로 출근','근무지 복귀만','바로 출근 후 근무지 복귀'],
  };
  all('input[name="trip-pattern"]',patterns).forEach(input=>{
    const wording=copy[input.value],label=input.closest('label');if(!wording||!label)return;
    input.setAttribute('aria-label',wording[2]);
    label.replaceChildren(input,make('strong','',wording[0]),make('small','',wording[1]));
  });
  const patternNote=make('p','ui-pattern-note');patterns.after(patternNote);
  function syncPattern(){
    patternNote.textContent={
      ROUND_TRIP:'근무지에서 출발해, 출장 후 근무지로 돌아옵니다.',
      OUTBOUND_ONLY_END_AFTER_SCHEDULE:'근무지 → 출장지만 계산합니다. 퇴근길은 제외합니다.',
      RETURN_ONLY_DIRECT_TO_DESTINATION:'출장지 → 근무지만 계산합니다. 집에서 출발하는 길은 제외합니다.',
    }[$('input[name="trip-pattern"]:checked',form)?.value]||'';
  }
  syncPattern();
  const originField=$('#origin-search')?.closest('.field-search');
  const destinationField=$('#destination-search')?.closest('.field-search');
  for(const [node,letter]of [[originField,'A'],[destinationField,'B']]){
    if(node){node.classList.add('ui-place-field');node.dataset.point=letter;}
  }
  if(originField)for(const n of [$('#institution-filters'),$('.facet-feedback')])if(n)originField.append(n);
  const endField=$('#returns-date')?.closest('.date-time-field');
  if(endField){
    const details=make('details','ui-end-details'),summary=make('summary',''),end=make('span','ui-end-time');
    end.id='ui-end-summary';summary.append(make('span','','종료 일시 확인·수정'),end,icon('chevron'));
    details.append(summary,endField);$('.schedule-fields').append(details);
  }
  const more=make('details','ui-more-options'),moreSummary=make('summary','');
  const optionState=make('small','ui-option-state','선택');
  moreSummary.append(make('span','','차량·추가 출장 설정'),optionState,icon('chevron'));more.append(moreSummary);
  const optionBody=make('div','ui-options-body');
  for(const n of [$('.vehicle-assumptions'),$('#other-trips')?.closest('label'),$('#previous-allowance-field'),$('#policy-disclosure')])if(n)optionBody.append(n);
  more.append(optionBody);calculate.before(more);
  const dock=make('div','ui-submit-dock'),error=$('#form-error'),hint=make('p','ui-submit-hint');
  hint.id='ui-submit-hint';hint.setAttribute('role','status');calculate.setAttribute('aria-describedby',hint.id);
  calculate.before(dock);if(error)dock.append(error);dock.append(calculate,hint);
  function syncSubmit(){
    const busy=calculate.textContent.includes('계산하고');
    if(/^(▣\s*)?경로 계산$/.test(calculate.textContent.trim()))calculate.textContent='경로·예상 여비 확인';
    calculate.setAttribute('aria-busy',String(busy));
    hint.textContent=busy?'경로 정보를 확인하고 있습니다.':calculate.disabled?'장소를 목록에서 선택하고 입력값을 확인해 주세요.':'결과는 예상값이며, 실제 지급액과 다를 수 있습니다.';
  }
  const bo=new MutationObserver(syncSubmit);
  bo.observe(calculate,{attributes:true,attributeFilter:['disabled'],childList:true,characterData:true,subtree:true});
  observers.push(bo);syncSubmit();
  function syncSchedule(){
    const end=$('#ui-end-summary');
    if(end){
      const startDate=$('#starts-date')?.value,date=$('#returns-date')?.value,time=$('#returns-time')?.value;
      end.textContent=date&&time?`${date===startDate?'당일':date.slice(5).replace('-','/')} ${time}`:'일정을 입력하세요';
    }
    const total=Number($('#duration-hours')?.value)*60+Number($('#duration-minutes')?.value);
    all('[data-duration-hours]').forEach(b=>b.setAttribute('aria-pressed',String(Number(b.dataset.durationHours)*60===total)));
    const vehicle=$('#vehicle-use'),other=$('#other-trips');
    const customized=(vehicle&&vehicle.value!=='NONE')||other?.checked;
    optionState.textContent=customized?'설정 적용 중':'선택';more.classList.toggle('has-options',Boolean(customized));syncPattern();
  }
  const syncAfterCore=()=>queueMicrotask(syncSchedule);
  for(const event of ['input','change','click'])form.addEventListener(event,syncAfterCore);
  window.addEventListener('load',syncSchedule,{once:true});syncSchedule();
  const toolbar=$('.map-toolbar');
  if(toolbar){
    const heading=$('strong',toolbar),note=$('p',toolbar);
    if(heading)heading.textContent='출장 경로';if(note)note.textContent='경로 선택 시 지도에 강조됩니다.';
  }
  const mapFooter=make('div','ui-map-footer');
  mapFooter.append(icon('pin'),make('span','','지도에서 출장지를 선택할 수도 있어요. 주소 후보를 확인해 주세요.'));
  $('.map-stage').append(mapFooter);
  // Keep the original button node; avoid resizing the private SDK canvas on mobile.
  $('#map-collapse')?.classList.add('ui-original-map-collapse');
  form.addEventListener('invalid',e=>{
    for(let n=e.target.parentElement;n&&n!==form;n=n.parentElement)if(n.tagName==='DETAILS')n.open=true;
  },true);
  window.addEventListener('pagehide',()=>observers.forEach(o=>o.disconnect()),{once:true});
})();
