<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { supabase } from '../../lib/supabase'
import { competition } from '../../stores/competition'
import { useAuth } from '../../stores/auth'
import { useI18n } from '../../composables/useI18n'
import { num } from '../../lib/format'
import { formatDateTime } from '../../lib/projectText'
import { useQuestFlags } from '../../composables/useQuestFlags'
import { scenarioLabel, scenarioOrder } from '../../lib/scenarioLabels'
import { elapsedText, runningMinutes, waitingForStage1, waitingHint, waitingText } from '../../lib/runProgress'
import { refreshDue, REFRESH_TICK_MS } from '../../lib/dashboardRefresh'
import { cancelEvaluation } from '../../lib/cancelEvaluation'
import { canCancel, cancelConfirmKey, cancelErrorKey } from '../../lib/projectEvaluation'
// `quiet` keeps the new-submission button secondary while the dashboard quest leads.
// `allPhases` is the records page: every complete-project evaluation with its scenario scores.
const props=withDefaults(defineProps<{limit?:number;quiet?:boolean;allPhases?:boolean;hideEmpty?:boolean}>(),{limit:50,quiet:false,allPhases:false,hideEmpty:false})
const {team}=useAuth(), {pick,t,locale}=useI18n(), {remember}=useQuestFlags()
type Run={id:string;status:string;score:number|null;started_at?:string|null;scenarios?:{slug:string;name:string}|null}
type Batch={id:string;status:string;score:number|null;created_at:string;quota_refunded?:boolean;repeat_group?:string|null;staged?:boolean
  phases?:{name_en:string;name_zh:string}|null;observer_runs?:Run[]}
const rows=ref<Batch[]>([]),loading=ref(true),error=ref(false)
let timer:number|undefined,lastLoadAt=0
const now=ref(Date.now())
const statuses=computed(()=>pick<Record<string,string>>({queued:'Queued',starting:'Starting',running:'Running',awaiting_csv:'Waiting for CSV',scored:'Scored',failed:'Failed',cancelled:'Cancelled'}, {queued:'排队中',starting:'启动中',running:'运行中',awaiting_csv:'等待 CSV',scored:'已评分',failed:'失败',cancelled:'已取消'}))
const runs=(b:Batch)=>[...(b.observer_runs??[])].sort((x,y)=>scenarioOrder(x.scenarios?.slug??'')-scenarioOrder(y.scenarios?.slug??''))
// Each card's live status: score once scored; queued A1–D1 of a two-stage evaluation wait for A–D; running cards show their time.
const runSlug=(run:Run)=>run.scenarios?.slug??''
const runHint=(b:Batch,run:Run)=>run.score==null&&waitingForStage1(b.staged,b.observer_runs??[],run,runSlug)?waitingHint(pick):''
function runText(b:Batch,run:Run){
  if(run.score!=null)return num(run.score)
  if(waitingForStage1(b.staged,b.observer_runs??[],run,runSlug))return waitingText(pick)
  const minutes=runningMinutes(run,now.value)
  return minutes==null?statuses.value[run.status]??run.status:elapsedText(pick,minutes)
}
async function load(){
  if(!team.value || (!props.allPhases && !competition.phaseId)){loading.value=false;return}
  lastLoadAt=Date.now()
  let request=supabase.from('observer_batches')
    .select((props.allPhases?'*,phases(name_en,name_zh),':'*,')+'observer_runs(id,status,score,started_at,scenarios(slug,name))')
    .eq('team_id',team.value.id).eq('purpose','formal')
  if(!props.allPhases)request=request.eq('phase_id',competition.phaseId!)
  const result=await request.order('created_at',{ascending:false}).limit(props.limit)
  error.value=Boolean(result.error)
  if(!result.error)rows.value=(result.data??[]) as unknown as Batch[]
  loading.value=false
}
// 取消排队: the batch being cancelled, and the outcome shown above the table.
const cancelling=ref(''),cancelNote=ref<{text:string;failed:boolean}|null>(null)
async function cancel(row:Batch){
  if(cancelling.value||!window.confirm(t(cancelConfirmKey(row))))return
  cancelling.value=row.id;cancelNote.value=null
  try{await cancelEvaluation(row.id);cancelNote.value={text:t('dash.cancel_eval.done'),failed:false}}
  catch(e){cancelNote.value={text:t(cancelErrorKey(e instanceof Error?e.message:'')),failed:true}}
  finally{cancelling.value='';await load()}
}
// Same cadence as the project dashboard: every 30 s while an evaluation is queued or running, else every 60 s.
onMounted(()=>{void load();timer=window.setInterval(()=>{
  if(Math.floor(Date.now()/60_000)!==Math.floor(now.value/60_000))now.value=Date.now()
  if(!document.hidden&&refreshDue({batches:rows.value},lastLoadAt,Date.now()))void load()
},REFRESH_TICK_MS)})
onUnmounted(()=>window.clearInterval(timer))
</script>
<template>
  <section v-if="!(props.hideEmpty && !error && !rows.length)" class="panel mb-6" data-testid="evaluation-history">
    <div class="hd"><h2>{{ props.allPhases ? pick('Complete-project evaluations','完整项目评测记录') : pick('Evaluations','评测记录') }}</h2><button class="btn sm" @click="load">{{ pick('Refresh','刷新') }}</button></div>
    <p v-if="props.allPhases" class="help">{{ pick('Each evaluation runs every scenario of its phase once; its score is the average (in the competition: the mean of cards A–D; A1–D1 count only on the super board). Evaluations that failed because of the platform are not counted toward the daily limit.','每次评测把该赛程全部场景各跑一遍，分数是这些场景的平均分（正式比赛为任务卡 A–D 的平均分，A1–D1 只计入超级总榜）；因平台原因失败的评测不计入每日次数。') }}</p>
    <p v-if="cancelNote" :class="cancelNote.failed ? 'errors' : 'text2'" role="status" data-testid="batch-cancel-outcome">{{ cancelNote.text }}</p>
    <p v-if="loading">{{ t('common.loading') }}</p>
    <p v-else-if="error" role="alert">{{ pick('Could not load evaluations. Please refresh.','无法加载评测记录，请刷新重试。') }}</p>
    <p v-else-if="!rows.length" class="text2">{{ pick('No evaluations yet.','还没有评测记录。') }}</p>
    <div v-else class="table-wrap"><table class="data-table">
      <thead><tr><th>{{ t('subs.when') }}</th><th v-if="props.allPhases">{{ t('subs.phase') }}</th><th>{{ t('common.status') }}</th><th>{{ props.allPhases ? pick('Average score','平均分') : t('subs.score') }}</th><th>{{ props.allPhases ? pick('Scenario scores','各场景得分') : pick('Cards','各卡状态') }}</th><th>{{ pick('Details','详情') }}</th></tr></thead>
      <tbody><tr v-for="row in rows" :key="row.id" :data-batch-id="row.id">
        <td class="m xs whitespace-nowrap">{{ formatDateTime(row.created_at, locale) }}</td>
        <td v-if="props.allPhases">{{ row.phases ? pick(row.phases.name_en,row.phases.name_zh) : '—' }}</td>
        <td>{{ statuses[row.status]??row.status }}<span v-if="row.quota_refunded" class="pill info ml-2" data-testid="batch-refunded">{{ pick('Not counted toward the daily limit','未计入次数') }}</span></td>
        <td class="m">{{ num(row.score) }}</td>
        <td class="m xs"><span v-for="(run,index) in runs(row)" :key="run.id" class="mr-3 inline-block" :title="runHint(row, run) || undefined" data-testid="run-live-status">{{ run.scenarios ? scenarioLabel(run.scenarios.slug, run.scenarios.name, locale) : pick(`Scenario ${index+1}`,`场景 ${index+1}`) }}: {{ runText(row, run) }}<span v-if="runHint(row, run)" class="sr-only"> {{ runHint(row, run) }}</span></span></td>
        <td><router-link class="accent-l" :to="'/compete?track=project#batch-'+row.id" @click="remember('review','competition')">{{ pick('View progress and results','查看进度与结果') }}</router-link>
          <button v-if="canCancel(row)" type="button" class="btn sm ml-2" :disabled="!!cancelling" :aria-busy="cancelling===row.id" data-testid="batch-cancel" @click="cancel(row)">{{ cancelling===row.id ? t('dash.cancel_eval.working') : t('dash.cancel_eval.button') }}</button></td>
      </tr></tbody>
    </table></div>
    <p v-if="!props.allPhases" class="mt-5"><router-link class="btn sm" :class="{ primary: !props.quiet }" to="/compete">{{ t('dash.new_submission') }} →</router-link></p>
  </section>
</template>
