import {setText,translate} from './i18n.js';
const seen=new Set();
const dialog=document.querySelector('#error-dialog');
let retryAction=null;
export function errorText(value){
  const text=typeof value==='string'?value:value?.message;
  return (typeof text==='string'&&text.trim()?text:'Произошла неизвестная ошибка. Загрузка не завершена. Повторите попытку; полученные части не удалены автоматически.')
    .replace(/https?:\/\/[^\s<>]+/gi,'[адрес источника]').slice(0,2000);
}
export function forgetError(key){seen.delete(key);}
export function reportError(value,{title='Не удалось выполнить действие',jobTitle='',key=null,retry=null}={}){
  if(key&&seen.has(key))return;
  if(key)seen.add(key);
  if(!dialog||typeof dialog.showModal!=='function'){window.alert(translate(title)+'\n\n'+(jobTitle?jobTitle+'\n\n':'')+translate(errorText(value)));return;}
  setText(document.querySelector('#error-title'),title);
  const jobNode=document.querySelector('#error-job-title');jobNode.hidden=!jobTitle;setText(jobNode,jobTitle,true);
  setText(document.querySelector('#error-detail'),errorText(value));
  retryAction=retry;
  document.querySelector('#error-retry').hidden=!retry;
  document.querySelector('#error-retry').disabled=false;
  if(!dialog.open)dialog.showModal();
}
document.querySelector('#error-close').onclick=()=>dialog.close();
document.querySelector('#error-retry').onclick=async()=>{
  const action=retryAction;if(!action)return;
  dialog.close();
  try{await action();}catch(error){reportError(error,{title:'Повторная попытка не удалась',retry:action});}
};
window.addEventListener('unhandledrejection',event=>{
  if(event.reason?.name==='AbortError')return;
  reportError(event.reason,{title:'Непредвиденная ошибка приложения'});
});
window.addEventListener('error',event=>{
  if(event instanceof ErrorEvent)reportError(event.error||event.message,{title:'Ошибка приложения',key:'runtime:'+event.message});
  else if(event.target?.tagName==='SCRIPT'||event.target?.tagName==='LINK')reportError('Не удалось загрузить компонент интерфейса. Обновите страницу.',{title:'Ошибка загрузки приложения',key:'app-resource'});
},true);
