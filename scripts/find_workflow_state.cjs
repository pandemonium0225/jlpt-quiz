// 由 Actions github-script 呼叫；基準只取完整成功部署，AI 狀態則包含失敗／待審的執行。
const fs = require('node:fs');

module.exports = async function findWorkflowState({github, context, core, writeFile = fs.writeFileSync}) {
  const scope = {...context.repo};
  const {data} = await github.rest.actions.listWorkflowRuns({
    ...scope, workflow_id: 'deploy.yml', branch: 'main', status: 'completed', per_page: 100,
  });
  let baseline = null, ai = null;
  let baselineStatus = {status:'missing', description:'尚無已保存的成功部署基準；本次建立基準'};
  // 重跑舊 run 時，其 updated_at 可能比新 run 還晚。
  const runs = data.workflow_runs.filter(run => run.id !== context.runId)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  let baselineResolved = false;
  for (const run of runs) {
    const artifacts = await github.paginate(github.rest.actions.listWorkflowRunArtifacts, {...scope, run_id:run.id, per_page:100});
    const latest = prefix => artifacts.filter(a => new RegExp(`^${prefix}-[0-9]+$`).test(a.name))
      .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
    if (!ai) {
      const artifact = latest('quiz-ai-state');
      if (artifact && !artifact.expired) ai = {run, artifact};
    }
    if (!baselineResolved && run.conclusion === 'success') {
      const artifact = latest('quiz-baseline');
      if (artifact) {
        baselineResolved = true;
        if (artifact.expired) {
          baselineStatus = {status:'expired', description:'上次成功部署的題庫保存期限已過；本次重新建立基準'};
        } else {
          baseline = {run, artifact};
          baselineStatus = {status:'available', description:`上次完整部署成功的 run ${run.id}，${run.updated_at}`, run_url:run.html_url};
        }
      }
    }
    if (baselineResolved && ai) break;
  }
  for (const [name, value] of [['baseline', baseline], ['ai', ai]]) {
    core.setOutput(`${name}_run`, value ? String(value.run.id) : '');
    core.setOutput(`${name}_artifact`, value ? value.artifact.name : '');
  }
  writeFile('.build/baseline-info.json', JSON.stringify(baselineStatus, null, 2));
  if (!ai) core.warning('沒有可用的 AI 快取／用量紀錄；首次啟用或 artifact 已過期。月費硬限制請另設於 API 專案。');
  return {baseline, ai, baselineStatus};
};
