'use strict';
const ArchiveTools = (() => {
  const normalize = value => String(value).normalize('NFKC').toLocaleLowerCase();
  function parseSearch(value) {
    let text=normalize(value), dates=[], invalid=false;
    const date=(year,month,day)=>{
      year=year?Number(year):null;month=month?Number(month):null;day=day?Number(day):null;
      if((year!==null&&(year<1900||year>2199))||(month!==null&&(month<1||month>12))||
          (day!==null&&(!month||day<1||day>new Date(Date.UTC(year||2000,month,0)).getUTCDate())))invalid=true;
      dates.push({year,month,day});return ' ';
    };
    text=text.replace(/(?<!\d)(\d{4})\s*[-/.年]\s*(\d{1,2})(?:\s*[-/.月]\s*(\d{1,2})\s*日?)?\s*月?(?!\d)/g,(_,y,m,d)=>date(y,m,d));
    text=text.replace(/(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)/g,(_,y,m,d)=>date(y,m,d));
    text=text.replace(/(?<!\d)(\d{4})(\d{2})(?!\d)/g,(_,y,m)=>date(y,m));
    text=text.replace(/(?<!\d)(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?(?!\d)/g,(_,m,d)=>date(null,m,d));
    text=text.replace(/(?<!\d)(\d{1,2})\s*月(?!\d)/g,(_,m)=>date(null,m));
    text=text.replace(/(?<!\d)((?:19|20|21)\d{2})\s*年?(?!\d)/g,(_,y)=>date(y));
    if(/^\s*\d{4}\s*$/.test(text)){const digits=text.trim();text=date(null,digits.slice(0,2),digits.slice(2));}
    return {dates,invalid,terms:text.trim().split(/\s+/).filter(Boolean)};
  }
  function matches(entry,query,category) {
    const parsed=typeof query==='string'?parseSearch(query):query;
    if(parsed.invalid)return false;
    const [year,month,day]=entry.started_at.slice(0,10).split('-').map(Number);
    if(!parsed.dates.every(d=>(!d.year||d.year===year)&&(!d.month||d.month===month)&&(!d.day||d.day===day)))return false;
    const text=normalize([entry.title,entry.started_at.slice(0,10),category,entry.summary.overview,
      ...entry.summary.topics,...entry.summary.tags].join(' '));
    return parsed.terms.every(term=>text.includes(term));
  }
  return {parseSearch,matches};
})();
if(typeof module!=='undefined')module.exports=ArchiveTools;
