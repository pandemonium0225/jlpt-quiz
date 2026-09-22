const PER_ROUND = 10;
const NO = ['①', '②', '③', '④'];
let BANK = [], current = [], picks = [], cursor = 0, graded = false;

const $ = id => document.getElementById(id);
const quiz = $('quiz'), nextBtn = $('next'), againBtn = $('again');

function escapeHtml(value){
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function japanese(value){
  // 先跳脫筆記內容，只有本地產生的 ruby／空格標籤可以成為 HTML。
  return escapeHtml(value)
    .replace(/([一-龯々〆ヶ]+)[（(]([ぁ-んァ-ヴー・\s]+)[）)]/g,
      '<ruby>$1<rp>（</rp><rt>$2</rt><rp>）</rp></ruby>')
    .replaceAll('［　］', '<span class="blank" aria-label="空格">［　］</span>');
}

// 文法題的 note 第一行與「來源」是同一個文法點標題，不重複顯示；
// 每題都一樣的但書降一級，不與解說本文同權重。
function noteLines(q){
  const source = String(q.source ?? '').trim();
  return String(q.note ?? '').split('\n')
    .map(line => line.trim()).filter(Boolean)
    .filter(line => line.replace(/^文法點：/, '').trim() !== source);
}

function validQuestion(q){
  return q && ['vocab','grammar','connect'].includes(q.kind)
    && typeof q.stem === 'string' && q.stem.trim()
    && typeof q.answer === 'string' && q.answer.trim()
    && Array.isArray(q.pool) && q.pool.every(p => typeof p === 'string')
    && new Set(q.pool.filter(p => p.trim() && p !== q.answer)).size >= 3;
}

function shuffle(a){
  const r = a.slice();
  for(let i = r.length - 1; i > 0; i--){
    const j = Math.floor(Math.random() * (i + 1));
    [r[i], r[j]] = [r[j], r[i]];
  }
  return r;
}

function draw(){
  // 各題型輪流抽取；同一原句一輪只出一次，避免互相洩漏答案。
  const groups = shuffle(['vocab', 'grammar', 'connect']).map(kind => shuffle(BANK.filter(q => q.kind === kind)));
  const picked = [], used = new Set();
  while(picked.length < PER_ROUND && groups.some(g => g.length)){
    for(const group of groups){
      while(group.length && picked.length < PER_ROUND){
        const q = group.pop();
        const key = (q.original || q.stem).replace(/（[ぁ-んァ-ヴー・\s]+）/g, '').replace(/\s/g, '');
        if(used.has(key)) continue;
        used.add(key);
        picked.push(q);
        break;
      }
    }
  }
  return shuffle(picked).map(q => {
    const distractors = shuffle([...new Set(q.pool.filter(p => p.trim() && p !== q.answer))]).slice(0, 3);
    const opts = shuffle([q.answer].concat(distractors));
    return {...q, opts, ans: opts.indexOf(q.answer)};
  });
}

// 答案欄：未答是細罫線，作答中加粗，答對朱色 ○，答錯墨色 ×。
function sheet(){
  const el = $('sheet');
  el.className = graded ? 'sheet big review' : 'sheet';
  el.innerHTML = current.map((q, i) => {
    const pick = picks[i];
    let cls = 'mk', glyph = '';
    if(pick === undefined){
      if(i === cursor && !graded) cls += ' now';
    } else if(pick === q.ans){
      cls += ' ok'; glyph = '○';
    } else {
      cls += ' ng'; glyph = '×';
    }
    if(graded && i === cursor) cls += ' now';
    const tag = graded && pick !== undefined ? 'button' : 'span';
    const attrs = tag === 'button'
      ? ` type="button" data-i="${i}" aria-label="回看第 ${i + 1} 題"`
      : ` aria-label="第 ${i + 1} 題"`;
    return `<${tag} class="${cls}"${attrs}>${glyph}</${tag}>`;
  }).join('');
}

function render(){
  graded = false;
  $('score').classList.remove('show');
  quiz.innerHTML = '';
  const q = current[cursor];
  if(!q) return;
  const sec = document.createElement('section');
  sec.className = 'q';
  sec.innerHTML = `
    <p class="kind">${escapeHtml(q.label)}</p>
    ${q.label === '用法' && q.source ? `<p class="gp" lang="ja">${japanese(q.source)}</p>` : ''}
    <p class="ask">${escapeHtml(q.instruction)}</p>
    <p class="stem" lang="ja">${japanese(q.stem)}</p>
    <ul class="opts">
      ${q.opts.map((o, j) => `
        <li><label class="opt" data-j="${j}">
          <input type="radio" name="opt" value="${j}">
          <span class="no">${NO[j]}</span>
          <span class="txt" lang="ja">${japanese(o)}</span>
          <span class="mark"></span>
        </label></li>`).join('')}
    </ul>
    <div class="exp">
      <h3>解答　${NO[q.ans]}　<span lang="ja">${japanese(q.answer)}</span></h3>
      ${q.original && q.original !== q.stem ? `<p lang="ja"><span class="lab">${q.punctuation_restored ? '補標點後的例句' : '原句'}</span>${japanese(q.original)}</p>` : ''}
      ${q.punctuation_restored && q.source_text ? `<details><summary>查看 Notion 原始段落（標點由程式補齊）</summary><p lang="ja">${escapeHtml(q.source_text)}</p></details>` : ''}
      ${q.translation ? `<p><span class="lab">翻譯</span>${escapeHtml(q.translation)}</p>` : ''}
      ${noteLines(q).map(line =>
        `<p${/不代表|不宣稱/.test(line) ? ' class="caveat"' : ''}>${japanese(line)}</p>`).join('')}
      ${q.source && q.label !== '用法' ? `<p><span class="lab">來源</span><span lang="ja">${japanese(q.source)}</span></p>` : ''}
      ${/^https:\/\/www\.notion\.so\/[a-f0-9]{32}(#[a-f0-9]{32})?$/.test(q.source_url || '') ? `<p><a href="${escapeHtml(q.source_url)}" target="_blank" rel="noopener noreferrer">開啟 Notion 筆記</a></p>` : ''}
    </div>`;
  quiz.appendChild(sec);
  sheet();
  if(picks[cursor] !== undefined) reveal();
  else{
    nextBtn.hidden = true;
    againBtn.hidden = true;
  }
  window.scrollTo({top: 0, behavior: 'smooth'});
}

// 標出正解與你的選擇；顏色只用在正解那一列。
function reveal(){
  const q = current[cursor], pick = picks[cursor];
  const sec = quiz.firstElementChild;
  if(!sec) return;
  sec.classList.add('done');
  sec.querySelectorAll('.opt').forEach(opt => {
    const j = Number(opt.dataset.j);
    opt.querySelector('input').disabled = true;
    const mark = opt.querySelector('.mark');
    if(j === q.ans){ opt.classList.add('right'); mark.textContent = '○'; }
    else if(j === pick){ opt.classList.add('wrong'); mark.textContent = '×'; }
    else opt.classList.add('dim');
  });
  const last = cursor === current.length - 1;
  nextBtn.hidden = graded;
  nextBtn.textContent = last ? '看結果' : '下一題';
  againBtn.hidden = !graded;
}

function choose(j){
  if(graded || picks[cursor] !== undefined) return;
  picks[cursor] = j;
  reveal();
  sheet();
}

function finish(){
  graded = true;
  const score = current.filter((q, i) => picks[i] === q.ans).length;
  const pct = score / current.length;
  quiz.innerHTML = '';
  $('scoreMsg').textContent = `${current.length} 題答對 ${score} 題。`;
  $('scoreHint').textContent =
    pct === 1 ? '這一輪的內容你已經掌握了。' :
    pct >= .7 ? '點上面的 × 可以回看錯的題目。' :
    pct >= .4 ? '建議回頭讀一次對應的筆記段落。' :
    '先把解說讀過一遍，再換一組題目練習。';
  $('score').classList.add('show');
  cursor = -1;
  sheet();
  nextBtn.hidden = true;
  againBtn.hidden = false;
  window.scrollTo({top: 0, behavior: 'smooth'});
}

function review(i){
  $('score').classList.remove('show');
  const keep = graded;
  cursor = i;
  render();
  graded = keep;
  reveal();
  sheet();
}

function startRound(){
  if(!BANK.length) return;
  current = draw();
  picks = [];
  cursor = 0;
  render();
}

quiz.addEventListener('click', e => {
  const label = e.target.closest('.opt');
  if(label) choose(Number(label.dataset.j));
});

$('sheet').addEventListener('click', e => {
  const btn = e.target.closest('.mk[data-i]');
  if(btn) review(Number(btn.dataset.i));
});

nextBtn.addEventListener('click', () => {
  if(cursor === current.length - 1) finish();
  else{ cursor++; render(); }
});

againBtn.addEventListener('click', startRound);

document.addEventListener('keydown', e => {
  if(e.metaKey || e.ctrlKey || e.altKey) return;
  if(e.key >= '1' && e.key <= '4'){ choose(Number(e.key) - 1); return; }
  if((e.key === 'Enter' || e.key === 'ArrowRight') && !nextBtn.hidden){
    e.preventDefault();
    nextBtn.click();
  }
});

fetch('quiz.json?v=' + Date.now())
  .then(r => {
    if(!r.ok) throw new Error(r.status);
    return r.json();
  })
  .then(data => {
    BANK = (Array.isArray(data.questions) ? data.questions : []).filter(validQuestion);
    // 題型名稱跟著題庫走：同一個 kind 底下有「用法」與「活用」兩種標籤。
    const counts = new Map();
    BANK.forEach(q => counts.set(q.label, (counts.get(q.label) ?? 0) + 1));
    const breakdown = [...counts].map(([label, n]) => `${label} ${n}`).join('、');
    $('meta').textContent =
      `題庫 ${BANK.length} 題（${breakdown}），`
      + `同步於 ${data.generated_at} 台灣時間。內容取自 Notion 筆記，每天重建一次。`;
    if(!BANK.length){
      quiz.innerHTML = '<p class="state">題庫是空的。檢查 Notion 分享權限與筆記格式，確認同類選項夠湊滿四個，再重建一次。</p>';
      return;
    }
    startRound();
  })
  .catch(err => {
    $('meta').textContent = '題庫讀取失敗。';
    quiz.innerHTML = `<p class="state">讀不到 quiz.json（${escapeHtml(err.message)}）。到 GitHub Actions 看最近一次建置是否成功。</p>`;
  });
