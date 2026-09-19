const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../site/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements = new Map();
const context = vm.createContext({
  document: {getElementById(id){
    if(!elements.has(id)) elements.set(id, {addEventListener(){}});
    return elements.get(id);
  }},
  // 不連線；測試抽題與呈現輔助函式。
  fetch: () => new Promise(() => {}),
});
vm.runInContext(script, context);

test('筆記 HTML 會被跳脫，日文讀音與空格仍能呈現', () => {
  const result = vm.runInContext(`japanese('<img src=x onerror=alert(1)>本（ほん）［　］')`, context);
  assert.ok(!result.includes('<img'));
  assert.ok(result.includes('&lt;img'));
  assert.ok(result.includes('<ruby>本<rp>（</rp><rt>ほん</rt>'));
  assert.ok(result.includes('aria-label="空格"'));
  assert.ok(vm.runInContext(`japanese('あまりに（も）')`, context).includes('あまりに（も）'));
});

test('拒絕題庫中缺少四個唯一選項的題目', () => {
  context.example = {kind:'vocab', stem:'本を［　］。', answer:'読む', pool:['書く','買う','売る']};
  assert.equal(vm.runInContext('validQuestion(example)', context), true);
  context.example.pool = ['読む','書く','書く','買う'];
  assert.equal(vm.runInContext('validQuestion(example)', context), false);
  context.example.pool = ['書く',{},'買う'];
  assert.equal(vm.runInContext('validQuestion(example)', context), false);
});

test('每輪平衡題型、四個唯一選項，且同一原句不重複', () => {
  context.fixtures = ['vocab','grammar','connect'].flatMap(kind =>
    Array.from({length:8}, (_, i) => ({kind, stem:`${kind}${i}`, original:`原句${i}`,
      answer:'本', pool:['本','猫','猫','犬','鳥','花']})));
  vm.runInContext('BANK = fixtures', context);
  const round = vm.runInContext('draw()', context);
  assert.equal(round.length, 8);
  assert.equal(new Set(round.map(q => q.original)).size, 8);
  for(const q of round){
    assert.equal(new Set(q.opts).size, 4);
    assert.equal(q.opts[q.ans], q.answer);
  }
  const counts = ['vocab','grammar','connect'].map(kind => round.filter(q => q.kind===kind).length);
  assert.ok(Math.max(...counts) - Math.min(...counts) <= 1);
});

test('少量及空題庫不會讓抽題卡住', () => {
  vm.runInContext('BANK = []', context);
  assert.equal(vm.runInContext('draw().length', context), 0);
  vm.runInContext('BANK = fixtures.slice(0, 2)', context);
  assert.equal(vm.runInContext('draw().length', context), 2);
});

test('補標點的例句與可展開的原始段落分開顯示，原文也會跳脫', () => {
  let rendered;
  context.document.getElementById('score').classList = {remove(){}};
  elements.get('quiz').appendChild = section => {rendered = section.innerHTML;};
  context.document.createElement = () => ({});
  context.document.querySelector = () => null;
  context.window = {scrollTo(){}};
  context.repairedQuestion = {kind:'vocab', label:'語彙', stem:'本を［　］。', answer:'読む',
    opts:['読む','書く','買う','売る'], ans:0, original:'本を読む。', punctuation_restored:true,
    source_text:'本を読む <script>alert(1)</script>'};
  vm.runInContext('current = [repairedQuestion]; render()', context);
  assert.ok(rendered.includes('補標點後的例句'));
  assert.ok(rendered.includes('<details><summary>查看 Notion 原始段落'));
  assert.ok(rendered.includes('&lt;script&gt;'));
  assert.ok(!rendered.includes('<script>'));
});
