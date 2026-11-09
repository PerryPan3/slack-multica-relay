function object(value:unknown):value is Record<string,unknown>{
  return !!value&&typeof value==='object'&&!Array.isArray(value);
}

/** Recognize the Relay adapter's complete block layout, never prose keywords. */
export function agentBody(raw:Record<string,unknown>):string|undefined {
  if(!raw.bot_id&&!raw.app_id)return;
  const blocks=raw.blocks;
  if(!Array.isArray(blocks)||blocks.length<2||!blocks.every(object))return;
  const last=blocks.at(-1)!;
  if(last.type!=='context'||typeof last.block_id!=='string'||!/^relay-delivery-[a-f0-9]{64}$/.test(last.block_id))return;
  const split=blocks.findIndex(block=>block.type==='context');
  if(split<1)return;
  const sections=blocks.slice(0,split);
  const body:string[]=[];
  for(const section of sections){
    if(section.type!=='section'||section.fields!==undefined||section.accessory!==undefined||!object(section.text)||!['mrkdwn','plain_text'].includes(String(section.text.type))||typeof section.text.text!=='string')return;
    body.push(section.text.text);
  }
  // Our adapter splits a body into 3000-codepoint chunks. Other multi-section
  // layouts may encode paragraph breaks that concatenation would lose.
  if(body.slice(0,-1).some(text=>Array.from(text).length!==3000))return;
  for(const context of blocks.slice(split)){
    if(context.type!=='context'||!Array.isArray(context.elements)||context.elements.length!==1)return;
    const element=context.elements[0];
    if(!object(element)||!['mrkdwn','plain_text'].includes(String(element.type))||typeof element.text!=='string')return;
  }
  const text=body.join('');
  return text.trim()?text:undefined;
}
