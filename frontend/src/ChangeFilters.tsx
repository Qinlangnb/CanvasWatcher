import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, json } from "./api";

type Filter = {id:number; enabled:boolean; removed:boolean; created_by:string; version:number; match_count:number; reason:string;
  rule:{course_id:number;source_name:string;change_type:string;field:string;operator:string;pattern:string};
  audit:{actor:string;action:string;version:number;reason:string;created_at:string}[]};
type Review = {change_id:number;status:string;reason:string;error?:string;matched_rule?:{id:number;version:number}|null};
export function ChangeFilters() {
  const query = useQuery({queryKey:['change-filters'],queryFn:()=>api<{rules:Filter[];recent_reviews:Review[]}>('/api/settings/change-filters')});
  const [busy,setBusy]=useState<number|null>(null), [feedback,setFeedback]=useState('');
  const gate=useRef(false);
  const [failed,setFailed]=useState(false);
  async function change(row:Filter,remove=false) {
    if(gate.current)return;
    gate.current=true;setBusy(row.id);setFeedback('');setFailed(false);
    try {await api(`/api/settings/change-filters/${row.id}`,remove ? {method:'DELETE'} : json('PATCH',{enabled:!row.enabled}));await query.refetch();setFeedback(remove?'Rule removed; its audit history is preserved.':'Filter updated.');}
    catch {setFailed(true);setFeedback('Filter could not be updated. Please retry.');} finally {gate.current=false;setBusy(null);}
  }
  return <details className="settings-panel change-filter-settings"><summary>Advanced · Important Changes review and noise filters</summary>
    <p>Only AI-approved changes appear on the homepage. Missing AI credentials or failed reviews never trigger a fallback notification. Learned rules suppress only repeated noise in one course/source; disabling a rule affects future reviews.</p>
    {query.isError && <p role="alert">Review diagnostics could not be loaded.</p>}
    {feedback && <p role={failed?'alert':'status'}>{feedback}</p>}
    {query.data && !query.data.rules.length && <p>No learned suppression rules.</p>}
    {query.data?.rules.map(row=><article key={row.id} className="filter-rule-card">
      <h3>Rule #{row.id} · {row.removed?'Removed':row.enabled?'Enabled':'Disabled'}</h3>
      <p>{row.created_by} · version {row.version} · {row.match_count} matches</p><p>{row.reason}</p>
      <p>Course #{row.rule.course_id} · {row.rule.source_name} · {row.rule.change_type}</p>
      <code>{row.rule.field} {row.rule.operator} {row.rule.pattern}</code>
      <div className="source-action-row"><button disabled={busy!==null} onClick={()=>void change(row)}>{busy===row.id?'Saving…':row.enabled?'Disable':'Enable / restore'}</button>
      {!row.removed && <button disabled={busy!==null} onClick={()=>void change(row,true)}>Remove</button>}</div>
      <details><summary>Audit history</summary><ul>{row.audit.map(a=><li key={a.version}>{a.created_at} · {a.actor} {a.action} · v{a.version}: {a.reason}</li>)}</ul></details>
    </article>)}
    <h3>Recent review diagnostics</h3><ul>{query.data?.recent_reviews.map(r=><li key={r.change_id}>Change #{r.change_id}: {r.status} · {r.reason}{r.matched_rule && ` · rule #${r.matched_rule.id} v${r.matched_rule.version}`}{r.error && ` · ${r.error}`}</li>)}</ul>
  </details>;
}
