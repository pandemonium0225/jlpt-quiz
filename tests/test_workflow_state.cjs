const {test} = require('node:test');
const assert = require('node:assert/strict');
const findState = require('../scripts/find_workflow_state.cjs');

const run = (id, conclusion = 'success') => ({id, conclusion, run_attempt:1, updated_at:`2026-09-${id}`, html_url:`https://example.test/${id}`});
const artifact = (name, expired = false) => ({name:`${name}-1`, expired, created_at:'2026-09-21'});

async function find(runs, artifacts) {
  const outputs = {}, files = {};
  const result = await findState({
    github: {
      rest: {actions: {
        listWorkflowRuns: async args => { assert.equal(args.status, 'completed'); return {data:{workflow_runs:runs}}; },
        listWorkflowRunArtifacts: () => {},
      }},
      paginate: async (_, {run_id}) => artifacts[run_id] ?? [],
    },
    context:{repo:{owner:'owner', repo:'repo'}, runId:30},
    core:{setOutput:(key,value) => {outputs[key] = value;}, warning:() => {}},
    writeFile:(path,data) => {files[path] = JSON.parse(data);},
  });
  return {outputs, files, result};
}

test('部署基準跳過失敗與待審 run；AI 進度取失敗 run 最新結果', async () => {
  const state = await find([run(23, 'failure'), run(22), run(21)], {
    23:[artifact('quiz-ai-state')], 22:[artifact('quiz-ai-state')], 21:[artifact('quiz-baseline'), artifact('quiz-ai-state')],
  });
  assert.equal(state.outputs.baseline_run, '21');
  assert.equal(state.outputs.ai_run, '23');
});

test('首次啟用不誤稱所有題目新增；基準過期不偷偷改用更舊基準', async () => {
  const first = await find([run(21)], {});
  assert.equal(first.files['.build/baseline-info.json'].status, 'missing');
  const expired = await find([run(23), run(21)], {
    23:[artifact('quiz-baseline', true), artifact('quiz-ai-state')], 21:[artifact('quiz-baseline')],
  });
  assert.equal(expired.outputs.baseline_run, '');
  assert.equal(expired.files['.build/baseline-info.json'].status, 'expired');
});

test('artifact API 出錯不可當作首次執行而歸零用量', async () => {
  await assert.rejects(findState({
    github:{rest:{actions:{listWorkflowRuns:async () => {throw new Error('403');}}}},
    context:{repo:{}}, core:{},
  }), /403/);
});

test('只重跑部署時仍可取回前一次 attempt 的 AI 用量', async () => {
  const retried = {...run(23), run_attempt:2};
  const state = await find([retried], {
    23:[artifact('quiz-ai-state'), {...artifact('quiz-baseline'), name:'quiz-baseline-2'}],
  });
  assert.equal(state.outputs.ai_artifact, 'quiz-ai-state-1');
  assert.equal(state.outputs.baseline_artifact, 'quiz-baseline-2');
});
