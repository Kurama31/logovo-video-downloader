let database;
export async function db() {
  if (!database) database = new Promise((resolve, reject) => {
    const request = indexedDB.open('dav-browser-v2', 2);
    request.onupgradeneeded = () => {
      if(!request.result.objectStoreNames.contains('jobs'))request.result.createObjectStore('jobs', {keyPath:'id'});
      if(!request.result.objectStoreNames.contains('settings'))request.result.createObjectStore('settings',{keyPath:'key'});
    };
    request.onblocked = () => reject(new Error('Для обновления локального хранилища закройте другие вкладки приложения и обновите страницу.'));
    request.onsuccess = () => {request.result.onversionchange=()=>request.result.close();resolve(request.result);};
    request.onerror = () => reject(request.error);
  });
  return database;
}
async function transaction(mode, operation, name='jobs') {
  const connection = await db();
  return new Promise((resolve,reject)=>{
    const tx=connection.transaction(name,mode);
    const request=operation(tx.objectStore(name));
    tx.oncomplete=()=>resolve(request.result);
    tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);
  });
}
export const saveLocal = record => transaction('readwrite', store=>store.put(record));
export const getLocal = id => transaction('readonly', store=>store.get(id));
export const allLocal = () => transaction('readonly', store=>store.getAll());
export const deleteLocal = id => transaction('readwrite', store=>store.delete(id));
export const getSetting = key => transaction('readonly',store=>store.get(key),'settings');
export const setSetting = (key,value) => transaction('readwrite',store=>store.put({key,...value}),'settings');
export const deleteSetting = key => transaction('readwrite',store=>store.delete(key),'settings');
export async function directory(id) {
  const root=await navigator.storage.getDirectory();
  const base=await root.getDirectoryHandle('dav-v2',{create:true});
  return base.getDirectoryHandle(id,{create:true});
}
export async function removeLocalFiles(id) {
  const root=await navigator.storage.getDirectory();
  const base=await root.getDirectoryHandle('dav-v2',{create:true});
  await base.removeEntry(id,{recursive:true}).catch(e=>{if(e.name!=='NotFoundError')throw e;});
}
export async function supportsStorage() {
  if(!window.isSecureContext || !navigator.storage?.getDirectory || !window.indexedDB)
    throw new Error('Для локальной сборки нужен современный браузер и HTTPS (либо localhost).');
  const root=await navigator.storage.getDirectory();
  const probe=await root.getFileHandle('dav-capability-check',{create:true});
  if(!probe.createWritable)throw new Error('Браузер не поддерживает запись локальных временных файлов. Обновите браузер.');
  const writer=await probe.createWritable();await writer.close();await root.removeEntry('dav-capability-check');
  await db();
}
