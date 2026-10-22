const assert=require('node:assert/strict');
const {parseSearch,matches}=require('../site/archive-utils.js');
const entry={title:'诶呀诶呀聊聊倒霉的一天',started_at:'2026-10-13T23:30:00+08:00',
  summary:{overview:'聊聊生活',topics:['搬家'],tags:['杂谈']}};
for(const query of ['2026-10-13','2026/10/13','2026.10.13','2026年10月13日','20261013',
  '10月13日','10/13','10-13','10.13','1013','2026-10','202610','2026年10月','10月','2026','２０２６／１０／１３',
  '10月13日 搬家','2026-10-13 杂谈','搬家 生活',''])assert(matches(entry,query,'杂谈'),query);
for(const query of ['2026-10-12','10月1日','10/3','2025','2026-13-13','2026-00-13',
  '2026-10-00','10/32','2026-02-29','10月13日 游戏','水仙'])assert(!matches(entry,query,'杂谈'),query);
const leap={...entry,started_at:'2024-02-29T00:00:00+08:00'};
assert(matches(leap,'2月29日','杂谈'));assert(matches(leap,'20240229','杂谈'));
assert(!matches(entry,'2月30日','杂谈'));
assert(parseSearch('9月8日 910').terms.includes('910'));
console.log('Archive date/search: 37 cases passed');
