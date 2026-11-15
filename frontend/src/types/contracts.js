/** @typedef {{id:number,code:string,station:string,planned_at:string,planned_day:string,actual_at:string|null,note:string,source_revision:number,revoked:boolean,source:string}} Arrival */
/** @typedef {{id:number,name:string,station:string,daily_at:string,recipient:string,enabled:boolean,next_run_at:string}} Subscription */
/** @typedef {{id:number,subscription_id:number,report_day:string,state:string,payload:object,review_note:string}} Receipt */
/** @typedef {{id:number,batch_key:string,source:string,status:'receiving'|'completed',input_count:number,applied_count:number,stale_count:number,duplicate_count:number,quarantined_count:number,balanced:boolean}} Batch */
/** @typedef {{id:number,batch_id:number,row_no:number,raw:object,status:'pending'|'applied'|'stale'|'duplicate'|'quarantined',reason:string,arrival_id:number|null,replay_of_item_id:number|null,resolved_by_item_id:number|null,resolution_status:string|null}} BatchItem */
