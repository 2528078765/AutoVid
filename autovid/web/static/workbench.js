/* Presentation helpers. Generation, assets and provider APIs remain in index.html. */
function setAppearance(theme, persist=true){
  const dark = theme === 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  const button = document.getElementById('themeToggle');
  const label = dark ? '切换到明亮主题' : '切换到暗色主题';
  button.title = label;
  button.setAttribute('aria-label', label);
  button.setAttribute('aria-pressed', String(dark));
  if (persist) try { localStorage.setItem('autovid.theme', dark ? 'dark' : 'light'); } catch (_) {}
}

function updateWorkbenchView(tab){
  tab = tab || document.querySelector('.tab.active')?.dataset.tab || 'create';
  const names = {
    create:['开始一条新的口播视频','准备文案、出镜素材和声音，剩下的交给工作流。','创作'],
    history:['历史作品与运行记录','查看成片、下载片段，或继续未完成的运行。','历史作品'],
    voices:['管理你的声音','录音或上传参考音频，之后直接复用。','音色库'],
    avatars:SHOW_PHOTO_FEATURES ? ['形象与出镜素材','照片与视频底片分别保存，按生成模式复用。','形象与素材'] : ['人物视频底片','上传一次出镜视频，之后可以反复用于不同文案。','视频底片'],
    settings:['连接生成服务','按语音和视频分类管理，创作页面选择已配置服务。','服务设置'],
  };
  const title = names[tab] || names.create;
  $('pageTitle').textContent = title[0];
  $('pageSubtitle').textContent = title[1];
  $('breadcrumb').textContent = '工作台 / ' + title[2];
  $('clearDraft').classList.toggle('hidden', tab !== 'create');
  $('clearDraft').disabled = RUN_BUSY;
  $('activeTaskHint').textContent = RUN_BUSY ? '正在生成 · 返回创作查看' : PENDING_GATE ? '等待人工确认 · 返回创作查看' : '';
  $('workflow').classList.toggle('hidden', tab !== 'create');
  const output = tab === 'create' || (tab === 'history' && !!RUN_ID);
  $('outputWorkspace').classList.toggle('hidden', !output);
  $('workspace').classList.toggle('wide', !output);
  $('nextStep').classList.toggle('hidden', tab !== 'create');
  const hasProgress = !!RUN_ID || RUN_BUSY || $('planbox').textContent.trim() !== '';
  $('progressCard').classList.toggle('hidden', !hasProgress);
  $('logCard').classList.toggle('hidden', !hasProgress);
  const phase = $('result').querySelector('video') ? 2 : (RUN_BUSY || PENDING_GATE ? 1 : 0);
  document.querySelectorAll('.workflow-item').forEach((el,i) => el.classList.toggle('active', i === phase));
}

function updateCreationGuide(){
  const count = $('script').value.replace(/\s/g, '').length;
  $('wordSummary').textContent = count ? `${count} 字 · 预计 ${Math.ceil(count / 4.5)} 秒（实际以配音为准）` : '0 字 · 填好文案后会估算时长';
  const sceneOnly = ((OPTION_INFO.avatar || {})[$('p_avatar').value] || {}).uses_scene_photo === true;
  const videoInput = usesSourceVideo();
  $('identityRow').classList.toggle('hidden', !SHOW_PHOTO_FEATURES || sceneOnly || videoInput);
  $('photoSourceBox').classList.toggle('hidden', !SHOW_PHOTO_FEATURES || videoInput);
  $('videoSourceBox').classList.toggle('hidden', SHOW_PHOTO_FEATURES && !videoInput);
  $('appearanceNote').textContent = videoInput || !SHOW_PHOTO_FEATURES ? '人物、衣服和动作来自视频底片' : '人物、衣服和背景来自照片';
  const errors = preflightProblems();
  $('readyText').textContent = RUN_BUSY ? '正在生成，可以切换页面查看素材和设置。' : errors.length ? `还需准备 ${errors.length} 项，查看右侧下一步。` : '内容已准备好，可以开始生成。';
  let message = RUN_BUSY ? '工作流正在执行，进度和结果会自动更新。' : PENDING_GATE ? '流程已暂停，请在确认面板中选择通过或重做。' : errors[0] || '点击开始生成；完成后在这里预览或下载视频。';
  $('nextAction').textContent = message;
  let target = '';
  const first = errors[0] || '';
  if (!RUN_BUSY && !PENDING_GATE){
    if (/音色|录音|直链|参考文字/.test(first)) target = 'voices';
    else if (/形象.*照片|没有选择形象|人物视频底片/.test(first)) target = 'avatars';
    else if (/接口|引擎|配置/.test(first)) target = 'settings';
  }
  $('nextActionButton').classList.toggle('hidden', !target);
  $('nextActionButton').textContent = target === 'settings' ? '去配置服务' : target === 'voices' ? '去准备音色' : '去准备形象';
  $('nextActionButton').onclick = () => jumpTo(target);
  const platform = {...(WORKBENCH_PLATFORM || {})};
  const mode = $('exportMode').value;
  if (mode === 'source' && SOURCE_VIDEO) Object.assign(platform,{width:SOURCE_VIDEO.width,height:SOURCE_VIDEO.height,fps:SOURCE_VIDEO.fps});
  else if (mode === 'portrait1080') Object.assign(platform,{width:1080,height:1920,fps:30});
  else if (mode === 'portrait720') Object.assign(platform,{width:720,height:1280,fps:30});
  $('previewFormat').textContent = `${platform.width || 1080} × ${platform.height || 1920}`;
  const spec = ((OPTION_INFO.avatar || {})[$('p_avatar').value] || {}).output;
  $('outputSpecs').textContent = videoInput || !SHOW_PHOTO_FEATURES
    ? `模型生成规格：由人物视频底片决定，当前模型没有独立的清晰度档位可选。${SOURCE_VIDEO ? `底片为 ${SOURCE_VIDEO.width} × ${SOURCE_VIDEO.height}，${SOURCE_VIDEO.fps} 帧。` : '请先选择底片查看具体规格。'}\n` +
      (mode === 'source' && !SOURCE_VIDEO ? '最终导出：默认跟随底片。' : `最终导出：${platform.width} × ${platform.height}，${platform.fps} 帧。`) +
      ' 修改导出规格不会增加云端原始画面的细节，模型实际返回规格会在运行时核验。'
    : `最终导出：${platform.width} × ${platform.height}，${platform.fps} 帧。${spec ? `模型生成规格：${spec.text}` : '模型规格待确认。'}`;
  updateWorkbenchView();
}

function displayScene(scene){
  SCENE = scene;
  $('sceneName').textContent = scene ? scene.name : '选择本次视频使用的照片';
  $('sceneDetails').textContent = scene ? `${scene.width} × ${scene.height} · 已保存在本地，可复用` : '已经保存的照片可以反复使用，无需每次重拍。';
  $('scenePreview').innerHTML = scene ? `<img src="${esc(scene.url)}" alt="本次出镜照片">` : '<div class="photo-empty">人物照片</div>';
  updateHints();
}

async function openScenePicker(){
  $('sceneDialog').showModal();
  $('sceneChoices').innerHTML = '<div class="empty">正在读取照片…</div>';
  $('sceneChoiceStatus').textContent = '';
  try {
    const response = await fetch('/api/scenes');
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '读取失败');
    const photos = data.photos || [];
    $('sceneChoices').innerHTML = photos.length ? photos.map((photo,i) => `<button type="button" class="scene-choice" data-scene-choice="${i}"><img src="${esc(photo.url)}" alt="${esc(photo.name)}" loading="lazy"><span>${esc(photo.name)}</span><span class="hint">${esc(photo.group)}</span></button>`).join('') : '<div class="empty">还没有已保存的照片。<br>上传出镜照片，或到形象与照片页保存素材。</div>';
    $('sceneChoices').querySelectorAll('[data-scene-choice]').forEach(button => button.onclick = async () => {
      button.disabled = true;
      $('sceneChoiceStatus').textContent = '正在检查照片…';
      try {
        const selected = photos[Number(button.dataset.sceneChoice)];
        const r = await fetch('/api/scene/select', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(selected.selection)});
        const scene = await r.json();
        if (!r.ok || scene.error) throw new Error(scene.error || '选择失败');
        if (scene.avatar_id && [...$('p_avatar_asset').options].some(o => o.value === scene.avatar_id)) $('p_avatar_asset').value = scene.avatar_id;
        displayScene(scene);
        $('sceneDialog').close();
        toast('照片已就绪，无需重新上传');
      } catch (error) { $('sceneChoiceStatus').textContent = error.message; }
      finally { button.disabled = false; }
    });
  } catch (error) {
    $('sceneChoices').textContent = '';
    $('sceneChoiceStatus').textContent = '无法读取照片：' + error.message;
  }
}

function initializeWorkbench(){
  setAppearance(document.documentElement.dataset.theme, false);
  $('themeToggle').onclick = () => setAppearance(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  window.addEventListener('storage', event => { if (event.key === 'autovid.theme') setAppearance(event.newValue, false); });
  $('panel-settings').appendChild($('recCard'));
  $('chooseScene').onclick = openScenePicker;
  $('uploadScene').onclick = () => $('sceneFile').click();
  $('closeSceneDialog').onclick = () => $('sceneDialog').close();
  $('clearDraft').onclick = () => {
    if (RUN_BUSY) return;
    if (($('script').value.trim() || $('topic').value.trim() || SCENE || SOURCE_VIDEO) && !confirm('清空当前文案和素材选择？已保存的素材与历史文件不会删除。')) return;
    $('script').value = ''; $('topic').value = '';
    $('addSubtitles').checked = true;
    resetHistoryView(); SOURCE_VIDEO = null; $('sourceVideoSelect').value = ''; $('sourceVideoPreview').removeAttribute('src'); $('sourceVideoPreview').classList.add('hidden'); $('sourceVideoStatus').textContent = '请选择人物视频底片'; displayScene(null); updateHints();
  };
  document.querySelectorAll('[data-script-source]').forEach(button => button.onclick = () => {
    SCRIPT_SOURCE = button.dataset.scriptSource;
    document.querySelectorAll('[data-script-source]').forEach(el => el.classList.toggle('active', el === button));
    $('scriptField').classList.toggle('hidden', SCRIPT_SOURCE !== 'script');
    $('topicField').classList.toggle('hidden', SCRIPT_SOURCE !== 'topic');
    updateHints();
  });
  $('script').oninput = updateHints; $('topic').oninput = updateHints;
  $('p_avatar').onchange = updateHints; $('p_script').onchange = updateHints;
  $('exportMode').onchange = updateHints;
  $('showPhotoFeatures').checked = SHOW_PHOTO_FEATURES;
  $('showPhotoFeatures').onchange = () => {
    SHOW_PHOTO_FEATURES = $('showPhotoFeatures').checked;
    try { localStorage.setItem('autovid.showPhotoFeatures',String(SHOW_PHOTO_FEATURES)); } catch (_) {}
    applyPhotoVisibility(); renderProviderSelects(); renderProviderList(SETTINGS_PROVIDERS); updateHints();
  };
  applyPhotoVisibility();
  initializeSourceVideos();
  updateWorkbenchView();
}

function applyPhotoVisibility(){
  $('photoLibrary').classList.toggle('hidden',!SHOW_PHOTO_FEATURES);
  $('materialLibraryTitle').textContent = SHOW_PHOTO_FEATURES ? '形象与出镜素材' : '人物视频底片';
  $('materialNavLabel').textContent = SHOW_PHOTO_FEATURES ? '形象与素材' : '视频底片';
  $('providerCatalog').classList.add('hidden');
  $('recCard').classList.add('hidden');
}

function usesSourceVideo(){
  return ((OPTION_INFO.avatar || {})[$('p_avatar').value] || {}).uses_source_video === true;
}

function displaySourceVideo(video){
  SOURCE_VIDEO = video;
  $('sourceVideoSelect').value = video ? video.name : '';
  const preview = $('sourceVideoPreview');
  preview.classList.toggle('hidden', !video);
  if (video) preview.src = video.url;
  else { preview.pause(); preview.removeAttribute('src'); }
  $('sourceVideoStatus').textContent = video ? `${video.name} · ${Number(video.duration).toFixed(2)} 秒 · ${video.width} × ${video.height} · 已保存，可复用` : '请选择人物视频底片';
  updateHints();
}

async function selectSourceVideo(name){
  const response = await fetch('/api/source-video/select', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  const video = await response.json();
  if (!response.ok || video.error) throw new Error(video.error || '选择底片失败');
  displaySourceVideo(video);
}

async function loadSourceVideos(){
  try {
    const response = await fetch('/api/source-videos');
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '读取失败');
    const videos = data.videos || [];
    $('sourceVideoSelect').innerHTML = '<option value="">选择底片，或上传新视频</option>' + videos.map(v => `<option value="${esc(v.name)}">${esc(v.name)}</option>`).join('');
    if (SOURCE_VIDEO) $('sourceVideoSelect').value = SOURCE_VIDEO.name;
    $('sourceVideoLibrary').innerHTML = videos.length ? videos.map(v => `<div class="asset" style="display:flex;gap:12px;align-items:center;justify-content:space-between;margin-top:10px"><span style="overflow-wrap:anywhere">${esc(v.name)}</span><button class="ghost tiny" data-use-source-video="${esc(v.name)}">用于创作</button></div>`).join('') : '<div class="empty">还没有视频底片，上传后可反复使用。</div>';
    $('sourceVideoLibrary').querySelectorAll('[data-use-source-video]').forEach(button => button.onclick = async () => {
      try { await selectSourceVideo(button.dataset.useSourceVideo); jumpTo('create'); toast(usesSourceVideo() ? '底片已选好' : '底片已选好，请选择视频口型替换服务后使用'); }
      catch (error) { toast(error.message); }
    });
  } catch (error) { $('sourceVideoLibrary').textContent = '读取底片失败：' + error.message; }
}

function initializeSourceVideos(){
  $('uploadSourceVideo').onclick = $('libraryUploadSourceVideo').onclick = () => $('sourceVideoFile').click();
  $('sourceVideoSelect').onchange = async () => {
    const name = $('sourceVideoSelect').value;
    if (!name) return displaySourceVideo(null);
    try { await selectSourceVideo(name); }
    catch (error) { displaySourceVideo(null); $('sourceVideoStatus').textContent = error.message; }
  };
  $('sourceVideoFile').onchange = async event => {
    const file = event.target.files[0]; event.target.value = '';
    if (!file) return;
    const status = text => { $('sourceVideoStatus').textContent = text; $('sourceVideoLibraryStatus').textContent = text; };
    if (file.size > 300 * 1024 * 1024) { status('底片不能超过 300MB'); return; }
    $('uploadSourceVideo').disabled = $('libraryUploadSourceVideo').disabled = true;
    status('正在上传并检查底片，请稍候…');
    try {
      const response = await fetch('/api/source-video?filename=' + encodeURIComponent(file.name), {method:'POST', headers:{'Content-Type':'application/octet-stream'}, body:file});
      const video = await response.json();
      if (!response.ok || video.error) throw new Error(video.error || '上传失败');
      await loadSourceVideos(); displaySourceVideo(video); status('底片已保存，可以反复使用。'); toast('人物视频底片已保存');
    } catch (error) { status('上传失败：' + error.message); }
    finally { $('uploadSourceVideo').disabled = $('libraryUploadSourceVideo').disabled = false; }
  };
  loadSourceVideos();
}
