import {Input, ALL_FORMATS, BlobSource, Output, StreamTarget, Mp4OutputFormat,
  WebMOutputFormat, MkvOutputFormat, EncodedPacketSink, EncodedVideoPacketSource,
  EncodedAudioPacketSource} from './vendor.js';

export async function remux(blobs, extension, handle, signal, onProgress) {
  const inputs=blobs.map(blob=>new Input({source:new BlobSource(blob),formats:ALL_FORMATS}));
  let output;
  const lanes=[];
  try {
    // Separate sources contribute one primary video/audio each; no decoding or translation.
    let hasVideo=false,hasAudio=false;
    for(const input of inputs){
      if(!hasVideo){const track=await input.getPrimaryVideoTrack();if(track){lanes.push({track,video:true});hasVideo=true;}}
      if(!hasAudio){const track=await input.getPrimaryAudioTrack();if(track){lanes.push({track,video:false});hasAudio=true;}}
    }
    if(!hasVideo)throw new Error('В загруженных данных не найдена видеодорожка.');
    const format=extension==='webm'?new WebMOutputFormat():extension==='mkv'?new MkvOutputFormat():new Mp4OutputFormat({fastStart:false});
    const writer=await handle.createWritable();
    output=new Output({format,target:new StreamTarget(writer,{chunked:true,chunkSize:1024*1024})});
    for(const lane of lanes){
      const codec=await lane.track.getCodec();
      if(!codec||!format.getSupportedCodecs().includes(codec))throw new Error('Исходный кодек не поддерживается для локальной сборки в '+extension.toUpperCase()+'.');
      lane.source=lane.video?new EncodedVideoPacketSource(codec):new EncodedAudioPacketSource(codec);
      const metadata={languageCode:await lane.track.getLanguageCode()};
      if(lane.video)metadata.rotation=await lane.track.getRotation();
      if(lane.video)output.addVideoTrack(lane.source,metadata);else output.addAudioTrack(lane.source,metadata);
      lane.config=await lane.track.getDecoderConfig();
      if(!lane.config)throw new Error('Не удалось прочитать параметры исходной дорожки.');
      lane.iterator=new EncodedPacketSink(lane.track).packets();
      lane.next=await lane.iterator.next();
    }
    const zero=Math.min(...await Promise.all(lanes.map(l=>l.track.getFirstTimestamp())));
    const duration=Math.max(...await Promise.all(lanes.map(l=>l.track.computeDuration())))-zero;
    await output.start();
    let last=0;
    while(lanes.some(l=>!l.next.done)){
      signal.throwIfAborted();
      const lane=lanes.filter(l=>!l.next.done).sort((a,b)=>a.next.value.timestamp-b.next.value.timestamp)[0];
      const packet=lane.next.value;
      await lane.source.add(packet.clone({timestamp:packet.timestamp-zero}),{decoderConfig:lane.config});
      if(performance.now()-last>200){onProgress(Math.min(.99,(packet.timestamp-zero)/Math.max(duration,.001)));last=performance.now();await new Promise(r=>setTimeout(r,0));}
      lane.next=await lane.iterator.next();
      if(lane.next.done)lane.source.close();
    }
    signal.throwIfAborted();
    await output.finalize();onProgress(1);
  } catch(e) {
    if(output && !['finalized','canceled'].includes(output.state))await output.cancel().catch(()=>{});
    throw e;
  } finally {
    for(const lane of lanes)await lane.iterator?.return().catch(()=>{});
    inputs.forEach(input=>input.dispose());
  }
}
