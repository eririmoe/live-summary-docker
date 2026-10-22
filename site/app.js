'use strict';
const $ = selector => document.querySelector(selector);
let archive = {entries: []}, activeCategory = '';
const pageSize=12,detailCache=new Map();
let filteredEntries=[],shown=0,detailRequest=0;
const categories=['杂谈','歌回','游戏','剧情朗读','同步视听','户外','联动','其他'];
function categoryOf(entry){if(categories.includes(entry.summary.category))return entry.summary.category;const text=entry.title+' '+entry.summary.tags.join(' ');const rules=[['歌回',/歌回|歌杂|歌会|音乐电台/i],['同步视听',/同步视听|一起看番|看番/i],['户外',/户外/i],['剧情朗读',/剧情朗读|视觉小说|水仙|Narcissu|连言连语/i],['游戏',/游戏|实况|植物大战僵尸|数字僵尸|教妹妹开车/i],['联动',/联动/i]];return rules.find(([,pattern])=>pattern.test(text))?.[0]||'杂谈';}
const node = (tag, text, className) => {const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;};
function arrowIcon(){const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg'),path=document.createElementNS(ns,'path');for(const [key,value] of Object.entries({class:'ui-icon',viewBox:'0 0 24 24',width:'18',height:'18','aria-hidden':'true',focusable:'false'}))svg.setAttribute(key,value);path.setAttribute('d','M7 17 17 7M7 7h10v10');svg.append(path);return svg;}
const clock = seconds => {const n=Math.max(0,Math.floor(seconds));return [Math.floor(n/3600),Math.floor(n%3600/60),n%60].map(v=>String(v).padStart(2,'0')).join(':');};
const minutes = seconds => seconds >= 3600 ? `${Math.floor(seconds/3600)} 小时 ${Math.round(seconds%3600/60)} 分钟` : `${Math.round(seconds/60)} 分钟`;
function timedSection(title,items,hotspots=false){if(!items?.length)return [];const list=node('ul',undefined,'timeline featured-clips');for(const item of items){const li=node('li'),content=node('div'),time=node('div',undefined,'clip-time');time.append(node('time',clock(item.start_seconds)),node('span','— '+clock(item.end_seconds)));content.append(node('h4',item.title),node('p',item.description),node('small',hotspots?`${item.danmaku_count.toLocaleString()} 条弹幕 · 分钟聚合`:({speech:'依据音轨定位',both:'音轨与弹幕',danmaku:'观众弹幕'})[item.source]));li.append(time,content);list.append(li);}return [node('h3',title),list];}
function empty(title,description){const box=node('div',undefined,'empty');box.append(node('span','◌'),node('h3',title),node('p',description));$('#entries').replaceChildren(box);}
async function detail(entry){
  const request=++detailRequest,body=$('#detail-body');
  body.replaceChildren(node('p','正在翻开这场直播的笔记…','detail-meta'));
  if(!$('#detail').open)$('#detail').showModal();
  history.replaceState(null,'','#live-'+entry.id);
  try{
    if(!detailCache.has(entry.id)){
      if(!/^\/data\/entries\/[a-f0-9]{64}\.json$/.test(entry.detail_url))throw new Error('path');
      const pending=fetch(entry.detail_url).then(response=>{if(!response.ok)throw new Error('load');return response.json();}).then(value=>{if(value.id!==entry.id||!value.summary?.timeline)throw new Error('format');return value;});
      detailCache.set(entry.id,pending);
      pending.catch(()=>detailCache.delete(entry.id));
    }
    const complete=await detailCache.get(entry.id);
    if(request===detailRequest&&$('#detail').open)showDetail(complete);
  }catch{
    if(request!==detailRequest||!$('#detail').open)return;
    const retry=node('button','重新加载','chip');retry.addEventListener('click',()=>detail(entry));
    body.replaceChildren(node('h2',entry.title),node('p','暂时没能打开这场笔记，请重试。'),retry);
  }
}
function showDetail(entry){
  const body=$('#detail-body'),s=entry.summary;body.replaceChildren();
  body.append(node('p',entry.started_at.slice(0,10).replaceAll('-',' / '),'eyebrow'),node('h2',entry.title),node('p',`${minutes(entry.duration_seconds)} · ${entry.danmaku_count.toLocaleString()} 条弹幕`,'detail-meta'),node('p',s.overview));
  if(s.topics.length){const ul=node('ul');s.topics.forEach(item=>ul.append(node('li',item)));body.append(node('h3','本场话题'),ul);}
  if(s.rewatch_segments?.length){body.append(...timedSection('值得回看的片段',s.rewatch_segments));body.append(node('p','时间点相对于本场录播起点，依据转写定位，可能存在少量偏移。','detail-meta'));}else if(s.highlights.length){const ul=node('ul');s.highlights.forEach(item=>ul.append(node('li',item)));body.append(node('h3','本场看点'),ul);}
  if(s.danmaku_hotspots?.length){body.append(...timedSection('弹幕热点',s.danmaku_hotspots,true));body.append(node('p','按已采集弹幕的两分钟窗口选择热点，时间精度为分钟；弹幕量不等于人数或观点共识。','detail-meta'));}
  if(s.recap_sections?.length){body.append(node('h3','详细回顾'));for(const chapter of s.recap_sections){const section=node('section',undefined,'recap-chapter');section.append(node('div',clock(chapter.start_seconds)+' — '+clock(chapter.end_seconds),'chapter-time'),node('h4',chapter.title));chapter.paragraphs.forEach(text=>section.append(node('p',text)));body.append(section);}}
  if(s.timeline.length){const list=node('ul',undefined,'timeline');for(const item of [...s.timeline].sort((a,b)=>a.start_seconds-b.start_seconds)){const li=node('li'),content=node('div');content.append(node('h4',item.title),node('p',item.description),node('small',({speech:'依据音轨',danmaku:'观众弹幕',both:'音轨与弹幕'})[item.source]));li.append(node('time',clock(item.start_seconds)),content);list.append(li);}body.append(node('h3','直播时间线'),list);}
  const caution=node('div',undefined,'caveats');caution.append(node('p','自动整理可能存在识别与概括误差，请以原录播为准。'));s.caveats.forEach(item=>caution.append(node('p',item)));body.append(caution);
  if(body.querySelector('h3')){const menu=node('div',undefined,'detail-nav');menu.setAttribute('role','navigation');menu.setAttribute('aria-label','本场总结章节');for(const title of ['本场话题','值得回看的片段','本场看点','弹幕热点','详细回顾','直播时间线']){const heading=[...body.querySelectorAll('h3')].find(h=>h.textContent===title);if(heading){const button=node('button',title);button.addEventListener('click',()=>heading.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'}));menu.append(button);}}body.insertBefore(menu,body.querySelector('h3'));}
  if(!$('#detail').open)$('#detail').showModal();
  history.replaceState(null,'','#live-'+entry.id);
}
function render(){
  const query=ArchiveTools.parseSearch($('#search').value);
  let entries=archive.entries.filter(e=>(!activeCategory||categoryOf(e)===activeCategory)&&ArchiveTools.matches(e,query,categoryOf(e)));
  if($('#sort').value==='old')entries=entries.slice().reverse();
  filteredEntries=entries;shown=0;$('#entries').replaceChildren();
  $('#browse-more').hidden=!entries.length;
  $('#result-status').textContent=`找到 ${entries.length} 场录播`;
  if(!entries.length){empty(archive.entries.length?'没有找到这一段记忆':'第一篇直播笔记，正在路上',archive.entries.length?'试试其他关键词，或切换话题。':'完成转写和内容整理后，直播总结会出现在这里。');return;}
  appendPage();
}
function appendPage(){
  const fragment=document.createDocumentFragment(),entries=filteredEntries.slice(shown,shown+pageSize);
  for(const entry of entries){const row=node('article',undefined,'entry'),date=node('div',undefined,'date'),d=entry.started_at.slice(0,10).split('-');date.append(node('strong',d[2]),node('span',`${d[0]}.${d[1]}`));const content=node('div'),h=node('h3'),button=node('button',entry.title);button.addEventListener('click',()=>detail(entry));h.append(button);const meta=node('div',undefined,'entry-meta');meta.append(node('span',minutes(entry.duration_seconds)),node('span','·'),node('span',`${entry.danmaku_count.toLocaleString()} 条弹幕`));meta.append(node('span',categoryOf(entry),'pill'));content.append(h,node('p',entry.summary.overview,'summary'),meta);const arrow=node('button',undefined,'entry-arrow');arrow.append(arrowIcon());arrow.setAttribute('aria-label',`阅读总结：${entry.title}`);arrow.addEventListener('click',()=>detail(entry));row.append(date,content,arrow);fragment.append(row);}
  $('#entries').append(fragment);shown+=entries.length;
  $('#browse-count').textContent=`已展示 ${shown} / ${filteredEntries.length} 场`;
  $('#load-more').hidden=shown>=filteredEntries.length;
}
$('#load-more').addEventListener('click',appendPage);
if('IntersectionObserver' in window){const observer=new IntersectionObserver(items=>{if(items.some(item=>item.isIntersecting)&&!$('#browse-more').hidden&&shown<filteredEntries.length)appendPage();},{rootMargin:'0px 0px 240px 0px'});observer.observe($('#load-more'));}
function renderTags(){const counts=new Map(categories.map(c=>[c,archive.entries.filter(e=>categoryOf(e)===c).length]));$('#tags').replaceChildren();for(const category of ['',...categories.filter(c=>counts.get(c))]){const label=(category||'全部')+' · '+(category?counts.get(category):archive.entries.length);const button=node('button',label,'chip');button.setAttribute('aria-pressed',String(category===activeCategory));button.addEventListener('click',()=>{activeCategory=category;renderTags();render();});$('#tags').append(button);}}
$('#search').addEventListener('input',render);$('#sort').addEventListener('change',render);
$('#close-detail').addEventListener('click',()=>$('#detail').close());$('#detail').addEventListener('close',()=>{detailRequest++;history.replaceState(null,'',location.pathname+location.search);});
$('#about').addEventListener('click',()=>$('#about-dialog').showModal());$('#close-about').addEventListener('click',()=>$('#about-dialog').close());
for(const dialog of document.querySelectorAll('dialog'))dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();}});
fetch('/data/index.json',{cache:'no-cache'}).then(response=>{if(!response.ok)throw new Error('load');return response.json();}).then(data=>{if(data.schema_version!==2||!Array.isArray(data.entries))throw new Error('format');archive=data;$('#entry-count').textContent=String(data.entries.length).padStart(2,'0');const total=data.entries.reduce((n,e)=>n+e.duration_seconds,0);$('#collection-note').textContent=data.entries.length?`${data.entries.length} 场录播 · ${minutes(total)} 的记忆`:'静候下一场相遇';$('#updated').textContent=`更新于 ${new Date(data.updated_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}`;renderTags();render();const id=location.hash.replace('#live-','');const entry=archive.entries.find(e=>e.id===id);if(entry)detail(entry);}).catch(()=>{empty('暂时没能打开档案','请稍后刷新页面重试。');$('#collection-note').textContent='档案暂时不可用';});
