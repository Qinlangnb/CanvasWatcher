export function sourceStatusTone(state:string){
 const value=state.toUpperCase().replaceAll("_"," ");
 if(value==="ACTIVE"||value==="HEALTHY"||value==="VALID")return"active";
 if(value==="READ ONLY"||value==="COMING SOON")return"neutral";
 if(value==="DEGRADED")return"warning";
 if(value==="AUTH REQUIRED"||value==="READY"||value==="PENDING RECONCILIATION"||value==="MISSING"||value==="DISABLED"||value==="VERIFYING")return"warning";
 return"danger";
}

export function SourceStatusBadge({state}:{state:string}){
 const normalized=state.toUpperCase().replaceAll("_"," ");
 return <span className={`source-status-badge source-status-pill ${sourceStatusTone(state)}`}><span className="status-dot"/>{normalized}</span>;
}
