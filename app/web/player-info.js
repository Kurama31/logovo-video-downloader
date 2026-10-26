export function playerInfo(value,{visible=true,pending=false}={}){
  const row=document.querySelector('#player-info');
  row.hidden=!visible;
  if(!visible){row.textContent='';row.removeAttribute('title');return;}
  const players=value?.players||[],libraries=value?.libraries||[],streams=value?.streams||[];
  let text=players.length?'Плеер: '+players.join(', '):value?.status==='direct'?'Плеер: не используется (прямая ссылка)':pending?'Плеер: определяем…':'Плеер: не определён';
  if(libraries.length)text+=' · Библиотека: '+libraries.join(', ');
  if(streams.length)text+=' · Поток: '+streams.join(', ');
  row.textContent=text;
  row.title=value?.note||'Распознавание плеера не гарантирует доступность скачивания.';
  row.dataset.state=pending&&!value?'analyzing':players.length?'detected':'unknown';
}
