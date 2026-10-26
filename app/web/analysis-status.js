const status=document.querySelector('#analysis-status');
const dots=document.querySelector('#analysis-dots');
const label=document.querySelector('#analysis-status-text');
const reason=document.querySelector('#analysis-reason');
const measure=document.createElement('canvas').getContext('2d');
let currentPercent=0;

function fitDots(){
  if(status.dataset.state==='idle'){dots.textContent='';return;}
  const style=getComputedStyle(dots);
  measure.font=style.font;
  const step=measure.measureText('.').width+(parseFloat(style.letterSpacing)||0);
  const capacity=Math.max(3,Math.floor(dots.getBoundingClientRect().width/Math.max(1,step)));
  const count=3+Math.floor((capacity-3)*currentPercent/100);
  dots.textContent='.'.repeat(count);
  dots.dataset.count=String(count);
  dots.dataset.capacity=String(capacity);
}

export function analysisStatus(state,percent=0,text=''){
  status.dataset.state=state;
  currentPercent=Math.max(0,Math.min(100,percent));
  status.dataset.percent=String(currentPercent);
  status.setAttribute('aria-busy',String(state==='analyzing'));
  label.textContent=state==='analyzing'?'Проверяем…':state==='idle'?(text||'Вставьте ссылку для анализа'):'Проверено';
  reason.hidden=state!=='error';
  reason.textContent=state==='error'?text:'';
  status.setAttribute('aria-label',state==='analyzing'?`Проверяем: завершено ${currentPercent}% этапов проверки`:state==='success'?'Проверено: видео можно скачать':state==='error'?'Проверено: '+text:label.textContent);
  fitDots();
}
new ResizeObserver(fitDots).observe(dots);
