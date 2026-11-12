// Frontend logic and HTML-template tests only. This does not launch or emulate a real browser.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{id,dataset:{},innerHTML:'',textContent:'',style:{},value:'',open:false,showModal(){this.open=true},close(){this.open=false},readOnly:false,classList:{toggle(){}},addEventListener(){},querySelectorAll(){return[]}});return elements.get(id)};
const nav=['overview','leads','content','tasks','settings'].map(tab=>({dataset:{tab},classList:{toggle(){}}}));
const goButtons=[{dataset:{go:'tasks'}}],jobButtons=[{dataset:{job:'model_check'}}],secretButtons=[{dataset:{clearSecret:'ai:api_key'}}];
let pendingFetch=null,confirmAnswer=true,confirmCalls=0;const requests=[];
const sandbox={console,setTimeout(){return 0},clearTimeout(){},setInterval(){return 0},confirm(){confirmCalls++;return confirmAnswer},prompt(){return null},document:{addEventListener(){},getElementById:element,querySelectorAll:s=>s==='nav button'?nav:s==='[data-go]'?goButtons:s==='[data-job]'?jobButtons:s==='[data-clear-secret]'?secretButtons:[]},fetch:async(url,options)=>{requests.push({url,options});return pendingFetch?await pendingFetch:{ok:true,json:async()=>structuredClone(fixture)}}};
sandbox.window=sandbox;
const context=vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(__dirname+'/web/app.js','utf8'),context);
const cases=[];
function test(name,fn){fn();cases.push({name,status:'passed'})}
function fillSettingsControls(){
 const s=fixture.settings;
 const values={'b-focus':s.business.search_focus,'b-voice':s.business.voice,'b-name':s.business.name,'b-offer':s.business.offer,'b-audience':s.business.audience,'b-facts':s.business.facts,'b-keywords':s.business.keywords.join('\n'),'b-topics':s.business.topics.join('\n'),'b-negatives':s.business.negative_keywords.join('\n'),'ai-url':s.ai.base_url,'ai-model':s.ai.model,'ai-key':'','ai-fallback-url':s.ai.fallback_base_url||'','ai-fallback-model':s.ai.fallback_model||'','ai-fallback-key':'','ai-limit':s.ai.calls_per_day,'search-mode':s.radar.search_mode,'xhs-url':s.radar.xhs_url,'xhs-token':'','max-queries':s.radar.max_queries,'max-results':s.radar.max_results,'max-details':s.radar.max_details,'search-limit':s.radar.searches_per_day,'post-limit':s.publishing.max_posts_per_day,'post-gap':s.publishing.min_gap_minutes,'slots':s.publishing.slots.join(','),'agent-minutes':s.autonomy.cycle_minutes,'agent-backlog':s.autonomy.max_pending_drafts,'agent-reply-limit':s.autonomy.replies_per_day,'scan-minutes':s.schedule.scan_minutes,'generate-time':s.schedule.generate_time};
 for(const [id,value] of Object.entries(values))element(id).value=String(value??'');
 for(const [id,value] of Object.entries({'ark-search':s.radar.ark_search_enabled,'xhs-enabled':s.radar.xhs_enabled,'pub-enabled':s.publishing.enabled,'auto-approve':s.publishing.auto_approve,'agent-enabled':s.autonomy.enabled,'agent-replies':s.autonomy.reply_to_own_comments,'schedule-enabled':s.schedule.enabled}))element(id).checked=value;
 for(const f of fixture.limit_fields){const control=element('limit-'+f.section+'-'+f.key);control.value=String(s[f.section][f.key]);control.dataset={limitSection:f.section,limitKey:f.key};}
}
async function main(){
 await new Promise(resolve=>setImmediate(resolve));
 test('author_contact_is_prominent_and_dialog_reopens',()=>{const html=fs.readFileSync(__dirname+'/web/index.html','utf8');assert.ok(html.includes('作者QQ：230154231'));assert.ok(html.includes('id="author-contact"'));assert.equal(element('author-dialog').open,true);element('author-dialog').close();vm.runInContext('showAuthorDialog()',context);assert.equal(element('author-dialog').open,true)});
 test('public_automation_has_separate_controls_without_sales_flow',()=>{const h=vm.runInContext('overview()+settingsView()',context);for(const label of ['开启全自动评论与发帖','只开启公开评论与回复','只分析，不发送','已人工接手','备用模型'])assert.ok(h.includes(label));assert.ok(!vm.runInContext('overview()',context).includes('实际成交'))});
 test('five_views_render_without_missing_data',()=>{for(const name of ['overview','leadsView','contentView','tasksView','settingsView'])assert.ok(vm.runInContext(name+'()',context).length>50)});
 test('detail_reading_limit_is_labeled_per_round',()=>{const h=vm.runInContext('settingsView()',context);assert.ok(h.includes('小红书每轮最多读取详情'));assert.ok(!h.includes('小红书每个词最多读取详情'))});
 test('sync_button_discloses_enabled_comment_replies',()=>{const h=vm.runInContext('settingsView()',context);assert.ok(h.includes('回查作品 / 按开关处理评论'));assert.ok(h.includes('此操作可能发送回复'));assert.ok(!h.includes('只回查小红书作品与互动'))});
 test('scan_summary_names_source_and_offers_xhs_switch',()=>{const html=vm.runInContext("(()=>{const scan=state.sales.scan,xhs=state.settings.radar.xhs_enabled,ark=state.settings.radar.ark_search_enabled;try{state.sales.scan={time:'2026-11-11T17:00:00+08:00',source:'ark_citations',queries:[]};state.settings.radar.xhs_enabled=false;state.settings.radar.ark_search_enabled=true;return scanSummary()}finally{state.sales.scan=scan;state.settings.radar.xhs_enabled=xhs;state.settings.radar.ark_search_enabled=ark}})()",context);assert.ok(html.includes('豆包联网引用'));assert.ok(html.includes('启用小红书站内搜索'));assert.ok(html.includes('没有执行站内搜索'))});
 test('own_post_import_and_explicit_comment_processing_are_visible',()=>{const h=vm.runInContext('contentView()',context);assert.ok(h.includes('接入本人已有作品与评论'));assert.ok(h.includes('data-job="process_comments"'));assert.ok(h.includes('真实回复'));});
 test('verified_reply_has_readable_status_and_source_proof',()=>{vm.runInContext("state.inbox=[{author:'合成访客',state:'verified',body:'合成原文',reply:'合成回复',proof:{reply_id:'fixture-reply',url:'https://www.xiaohongshu.com/explore/fixture-own',checked_at:'2026-11-11T15:00:00+08:00'}}]",context);const h=vm.runInContext('autonomyPanel()',context);assert.ok(h.includes('已在原评论下回查确认'));assert.ok(h.includes('查看原评论与回复'));assert.ok(h.includes('fixture-own'));vm.runInContext('state.inbox=[]',context)});
 test('untrusted_text_is_escaped',()=>{assert.equal(vm.runInContext('esc("<script>\\\"&")',context),'&lt;script&gt;&quot;&amp;')});
 test('sales_workflow_has_separate_tests_and_model_guidance',()=>{const h=vm.runInContext('tasksView()+settingsView()',context);for(const word of ['测试模型接口','测试写作能力','检查小红书账号','检查发稿准备','检查已有图片','401/403'])assert.ok(h.includes(word));});
 test('old_diagnostics_are_folded_and_lead_filters_exist',()=>{const h=vm.runInContext('overview()+leadsView()',context);assert.ok(h.includes('<details><summary>历史综合检查'));assert.ok(h.includes('lead-filter'));assert.ok(h.includes('待判断'));});
 test('only_xiaohongshu_is_available',()=>{const html=vm.runInContext('settingsView()+overview()',context);assert.ok(html.includes('小红书'));for(const label of ['抖音','快手','知乎','B站','视频号','Tavily','social-auto-upload'])assert.ok(!html.includes(label));assert.equal(vm.runInContext('Object.keys(platforms).join() ',context),'xiaohongshu')});
 test('review_and_deferred_states_have_clear_labels',()=>{assert.ok(vm.runInContext("badge('needs_review')",context).includes('待核对'));assert.ok(vm.runInContext("badge('deferred')",context).includes('已自动顺延'))});
 test('settings_contains_card_and_secret_recovery_controls',()=>{const html=vm.runInContext('settingsView()',context);assert.ok(html.includes('data-clear-secret'));assert.ok(html.includes('rebind-xhs'))});
 test('provider_templates_are_available_without_credentials',()=>{const html=vm.runInContext('settingsView()',context);for(const name of ['ai-preset','DeepSeek','OpenAI','百炼','兼容接口'])assert.ok(html.includes(name));assert.ok(!JSON.stringify(fixture.model_presets).includes('api_key'))});
 test('switching_provider_clears_typed_key_and_disables_ark_search_without_request',()=>{element('ai-key').value='fixture-typed-key';element('ark-search').checked=true;vm.runInContext("applyModelPreset('deepseek')",context);assert.equal(element('ai-url').value,'https://api.deepseek.com');assert.equal(element('ai-key').value,'');assert.equal(element('ark-search').checked,false);assert.equal(vm.runInContext('settingsDirty',context),true)});
 test('custom_provider_allows_manual_address_and_model',()=>{vm.runInContext("applyModelPreset('custom')",context);assert.equal(element('ai-url').value,'');assert.equal(element('ai-model').value,'')});
 test('all_limit_fields_are_manually_editable',()=>{const html=vm.runInContext('limitSettings()',context);assert.ok(html.includes('全部设为无限制并保存'));assert.ok(html.includes('limits-form'));assert.ok(fixture.limit_fields.some(f=>f.section==='public'&&f.key==='comments_per_day'));assert.ok(fixture.limit_fields.some(f=>f.section==='public'&&f.key==='own_watch_minutes'));});
 vm.runInContext('settingsDirty=false;limitsDirty=false',context);await vm.runInContext('saveLimits(null,true)',context);
 test('one_click_unlimited_sends_count_minus_one_and_interval_zero',()=>{const req=requests.filter(r=>r.url==='/api/settings').at(-1);const body=JSON.parse(req.options.body);for(const f of fixture.limit_fields)assert.equal(body[f.section][f.key],f.kind==='interval'?0:-1);});
 test('unlimited_counts_do_not_show_exhausted_banner',()=>{assert.equal(vm.runInContext("state.settings.ai.calls_per_day=-1;state.settings.radar.searches_per_day=-1;state.stats.calls=999;state.stats.searches=999;usageHelp().includes('已用完')",context),false);});
 vm.runInContext("tab='settings';settingsDirty=true;document.getElementById('view').innerHTML='unsaved-input-marker'",context);
 await vm.runInContext('refresh()',context);
 test('polling_does_not_replace_unsaved_settings',()=>assert.equal(element('view').innerHTML,'unsaved-input-marker'));
 test('settings_shortcut_and_navigation_keep_edits_when_cancelled',()=>{
  vm.runInContext("tab='settings';settingsDirty=true;limitsDirty=true",context);element('b-name').value='unsaved-business';element('view').innerHTML='unsaved-both-forms';confirmAnswer=false;const before=confirmCalls;
  goButtons[0].onclick();nav.find(b=>b.dataset.tab==='tasks').onclick();
  assert.equal(confirmCalls,before+2);assert.equal(vm.runInContext('tab',context),'settings');assert.equal(element('b-name').value,'unsaved-business');assert.equal(element('view').innerHTML,'unsaved-both-forms');assert.equal(vm.runInContext('settingsDirty&&limitsDirty',context),true);
 });
 test('confirmed_navigation_discards_both_dirty_states',()=>{confirmAnswer=true;goButtons[0].onclick();assert.equal(vm.runInContext('tab',context),'tasks');assert.equal(vm.runInContext('settingsDirty||limitsDirty',context),false);nav.find(b=>b.dataset.tab==='settings').onclick();assert.equal(vm.runInContext('settingsDirty||limitsDirty',context),false);});
 fillSettingsControls();vm.runInContext("tab='settings';settingsDirty=false;limitsDirty=false;bindView()",context);
 element('limit-ai-calls_per_day').value='333';element('limits-form').oninput({target:element('limit-ai-calls_per_day')});
 element('max-results').value='88';element('settings-form').oninput({target:element('max-results')});
 element('limit-ai-analysis_per_run').value='45';element('limits-form').oninput({target:element('limit-ai-analysis_per_run')});
 element('b-name').value='combined-business-fixture';element('settings-form').oninput({target:element('b-name')});
 const synchronized=[element('ai-limit').value,element('limit-radar-max_results').value];
 await element('settings-form').onsubmit({preventDefault(){}});
 test('business_save_preserves_both_forms_and_latest_limit_inputs',()=>{const req=requests.filter(r=>r.url==='/api/settings').at(-1),body=JSON.parse(req.options.body);assert.deepEqual(synchronized,['333','88']);assert.equal(body.business.name,'combined-business-fixture');assert.equal(body.ai.calls_per_day,333);assert.equal(body.radar.max_results,88);assert.equal(body.ai.analysis_per_run,45);assert.equal(vm.runInContext('settingsDirty||limitsDirty',context),false);});
 fillSettingsControls();vm.runInContext("settingsDirty=true;limitsDirty=true",context);element('limit-ai-analysis_per_run').value='1.5';element('b-name').value='invalid-limit-unsaved';
 const beforeInvalid=requests.length;await element('settings-form').onsubmit({preventDefault(){}});
 test('invalid_upper_limit_keeps_both_forms_without_partial_save',()=>{assert.equal(requests.length,beforeInvalid);assert.equal(element('b-name').value,'invalid-limit-unsaved');assert.equal(element('limit-ai-analysis_per_run').value,'1.5');assert.equal(vm.runInContext('settingsDirty&&limitsDirty',context),true);});
 element('limit-ai-analysis_per_run').value='23';pendingFetch={ok:false,json:async()=>({error:'fixture save failure'})};await element('settings-form').onsubmit({preventDefault(){}});pendingFetch=null;
 test('failed_combined_save_keeps_business_and_limit_edits',()=>{assert.equal(element('b-name').value,'invalid-limit-unsaved');assert.equal(element('limit-ai-analysis_per_run').value,'23');assert.equal(vm.runInContext('settingsDirty&&limitsDirty',context),true);});
 vm.runInContext("settingsDirty=false;limitsDirty=true;tab='settings'",context);const beforeJob=requests.length;await jobButtons[0].onclick();
 test('unsaved_limits_prevent_checks_from_discarding_the_form',()=>{assert.equal(requests.length,beforeJob);assert.equal(vm.runInContext('tab',context),'settings');assert.equal(vm.runInContext('limitsDirty',context),true);});
 const beforeClear=requests.length,clearSnapshots=[];
 for(const flags of ['settingsDirty=true;limitsDirty=false','settingsDirty=false;limitsDirty=true']){
  vm.runInContext(flags+";tab='settings';bindView()",context);element('b-name').value='pending-before-clear';element('limit-ai-analysis_per_run').value='64';element('view').innerHTML='pending-before-clear-marker';
  await secretButtons[0].onclick();clearSnapshots.push({dirty:vm.runInContext('settingsDirty||limitsDirty',context),business:element('b-name').value,limit:element('limit-ai-analysis_per_run').value,view:element('view').innerHTML});
 }
 test('credential_clear_preserves_pending_business_and_limit_edits',()=>{assert.equal(requests.length,beforeClear);for(const snapshot of clearSnapshots){assert.equal(snapshot.dirty,true);assert.equal(snapshot.business,'pending-before-clear');assert.equal(snapshot.limit,'64');assert.equal(snapshot.view,'pending-before-clear-marker');}});
 console.log(JSON.stringify({scope:'frontend_logic_not_browser_e2e',passed:cases.length,cases},null,2));
}
main().catch(error=>{console.error(error);process.exitCode=1});
