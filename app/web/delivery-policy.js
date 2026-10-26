export function deliveryContext(navigator=globalThis.navigator,document=globalThis.document){
  return {activation:typeof navigator?.userActivation?.isActive==='boolean'?navigator.userActivation.isActive:null,
    visible:document?.visibilityState==='visible'};
}
export function deliveryErrorName(error){
  return ['NotAllowedError','SecurityError','AbortError','TypeError','InvalidStateError','DataError'].includes(error?.name)?error.name:'OtherError';
}
// A download link has no completion/blocked callback. Dispatch is NOT proof of saving.
export function attemptAutomaticDownload({url,name,document=globalThis.document,navigator=globalThis.navigator,emit=()=>{}}){
  const context=deliveryContext(navigator,document);
  emit('attempt',context);
  let link;
  try{
    link=document.createElement('a');link.href=url;link.download=name;
    document.body.append(link);link.click();
    emit('command_sent',context);
    return {status:'unconfirmed',errorName:null};
  }catch(error){
    const errorName=deliveryErrorName(error);emit('error',{...context,error_name:errorName});
    return {status:'error',errorName};
  }finally{link?.remove();}
}
export function sharePreparedFile({file,navigator=globalThis.navigator,document=globalThis.document,emit=()=>{}}){
  const context=deliveryContext(navigator,document);
  emit('attempt',context);
  // Call share synchronously in the click handler, before any await/log request.
  let action;
  try{action=navigator.share({files:[file]});}catch(error){action=Promise.reject(error);}
  return Promise.resolve(action).then(()=>{
    emit('handed_off',context);return {status:'handed_off'};
  },error=>{
    const errorName=deliveryErrorName(error),cancelled=errorName==='AbortError';
    emit(cancelled?'cancelled':'error',{...context,error_name:errorName});
    return {status:cancelled?'cancelled':'error',errorName};
  });
}
