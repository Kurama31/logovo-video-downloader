import {setText,setAttributeText} from './i18n.js';
import {api,safeName} from './downloader.js';
import {getSetting,setSetting,deleteSetting} from './local-store.js';
import {reportError} from './error-dialog.js';
import {preferencesAllowed,removePreferenceCookie} from './consent.js';

export let destination={mode:'browser',label:'Папка загрузок браузера'};
let folder=null;
let preferenceSync=Promise.resolve();
const cookieName='dav_destination';
function writeCookie(){
  if(!preferencesAllowed()){removePreferenceCookie(cookieName);return;}
  document.cookie=`${cookieName}=${encodeURIComponent(JSON.stringify(destination))}; Max-Age=34560000; Path=/; SameSite=Lax${location.protocol==='https:'?'; Secure':''}`;
}
function display(){
  const label=document.querySelector('#destination-label');
  setText(label,destination.mode==='browser'?'Папка загрузок браузера':destination.label,destination.mode!=='browser');
  setAttributeText(label,'title',destination.mode==='browser'?'Используется папка из настроек браузера. Её полный путь сайт получить не может.':'Выбранная папка: '+destination.label+'. Браузер не раскрывает абсолютный путь.');
  setText(document.querySelector('#destination-note'),destination.mode==='directory'?'Папка выбрана. Полный путь браузер не раскрывает.':'Папка из настроек браузера; полный путь недоступен сайту.');
}
async function persist(value,handle){
  // Persist permission locally. The server and cookie receive only a descriptive label.
  destination=value;folder=handle;writeCookie();display();
  await syncRememberedDestination();
}
async function clearRememberedDestination(){
  removePreferenceCookie(cookieName);await deleteSetting('destination');
  await api('preferences','DELETE');
}
function syncRememberedDestination(){
  preferenceSync=preferenceSync.catch(()=>{}).then(async()=>{
    if(!preferencesAllowed()){await clearRememberedDestination();return;}
    await setSetting('destination',{mode:destination.mode,label:destination.label,handle:folder});
    if(!preferencesAllowed()){await clearRememberedDestination();return;}
    writeCookie();await api('preferences','POST',destination);
    if(!preferencesAllowed())await clearRememberedDestination();
  });
  return preferenceSync;
}
export async function initDestination(){
  const canChoose=typeof window.showDirectoryPicker==='function';
  document.querySelector('#destination-choose').hidden=!canChoose;
  document.querySelector('#destination-button').hidden=!canChoose;
  document.querySelector('.destination-row').classList.toggle('is-browser-only',!canChoose);
  window.addEventListener('dav-consent-change',()=>{
    syncRememberedDestination().catch(e=>reportError(e,{title:'Не удалось обновить сохранение настроек'}));
  });
  const raw=preferencesAllowed()?document.cookie.split('; ').find(v=>v.startsWith(cookieName+'='))?.slice(cookieName.length+1):null;
  if(raw)try{const parsed=JSON.parse(decodeURIComponent(raw));if((parsed.mode==='browser'||parsed.mode==='directory'&&canChoose)&&typeof parsed.label==='string'&&parsed.label.length<=255)destination=parsed;}catch{}
  writeCookie();display();
  try{
    if(preferencesAllowed()){
      const remote=await api('preferences');
      const local=preferencesAllowed()?await getSetting('destination'):null;
      if(preferencesAllowed()){
        if(remote.mode==='directory'&&canChoose){
          destination=remote;
          if(local?.mode==='directory'&&local.label===remote.label)folder=local.handle||null;
        }else destination={mode:'browser',label:'Папка загрузок браузера'};
        writeCookie();display();
      }
    }
    await syncRememberedDestination();
  }catch(e){setText(document.querySelector('#destination-note'),'Настройка показана из cookie. Не удалось синхронизировать с сервером.');reportError(e,{title:'Не удалось синхронизировать папку сохранения',key:'destination-sync'});}
  const button=document.querySelector('#destination-button');
  const menu=document.querySelector('#destination-menu');
  button.onclick=()=>{menu.hidden=!menu.hidden;button.setAttribute('aria-expanded',String(!menu.hidden));};
  document.querySelector('#destination-choose').onclick=async()=>{
    try{
      if(!window.showDirectoryPicker)throw new Error('Этот браузер не поддерживает выбор папки сайтом. Измените папку в настройках загрузок браузера.');
      const handle=await window.showDirectoryPicker({id:'dav-download-folder',mode:'readwrite',startIn:'downloads'});
      await persist({mode:'directory',label:handle.name},handle);
      menu.hidden=true;button.setAttribute('aria-expanded','false');
    }catch(e){if(e?.name!=='AbortError')reportError(e,{title:'Не удалось выбрать папку'});}
  };
  document.querySelector('#destination-default').onclick=async()=>{
    try{await persist({mode:'browser',label:'Папка загрузок браузера'},null);menu.hidden=true;button.setAttribute('aria-expanded','false');}
    catch(e){reportError(e,{title:'Не удалось синхронизировать настройку папки'});}
  };
  document.addEventListener('click',event=>{if(!event.target.closest('.destination-row')){menu.hidden=true;button.setAttribute('aria-expanded','false');}});
}

export async function destinationFile(job){
  if(destination.mode==='browser'||typeof window.showDirectoryPicker!=='function')return null;
  if(!folder)throw new Error('Папка запомнена, но разрешение на неё отсутствует. Нажмите «…» и выберите её заново либо вернитесь к папке браузера.');
  if(folder.requestPermission&&await folder.requestPermission({mode:'readwrite'})!=='granted')throw new Error('Нет разрешения на запись в выбранную папку. Выберите папку через «…».');
  const create=async()=>{
    const original=safeName(job.title,job.extension);
    const dot=original.lastIndexOf('.'),stem=original.slice(0,dot),ext=original.slice(dot);
    for(let index=0;index<1000;index++){
      const name=index?`${stem} (${index})${ext}`:original;
      try{await folder.getFileHandle(name);}catch(e){if(e.name!=='NotFoundError')throw e;return folder.getFileHandle(name,{create:true});}
    }
    throw new Error('Слишком много файлов с одинаковым именем в выбранной папке.');
  };
  return navigator.locks?navigator.locks.request('dav-output-filename',create):create();
}
