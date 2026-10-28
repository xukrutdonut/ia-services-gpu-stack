'use strict';

// ---------------------------------------------------------------------------
// Estado
// ---------------------------------------------------------------------------
const S = {
  mode: 'new',
  course: { id: null, fullname: '', shortname: '', category_id: '', summary: '', format: 'topics', startSection: 1 },
  syl: { topic: '', audience: 'Residentes de pediatría y neuropediatría', num_modules: 5, outcomes: '', qpm: 5 },
  modules: [],
  run: { model: 'intel', temp: 0.3, max_tokens: 3800, publish: true },
  jobId: null,
  job: null,
  edits: {},
};

const TABS = {
  course:   ['Curso y destino', 'Elige si crear un curso nuevo o trabajar sobre uno existente.'],
  syllabus: ['Temario', 'Genera la estructura con IA local o edítala módulo a módulo.'],
  run:      ['Generación', 'Ejecuta el plan contra Moodle con el modelo local.'],
  review:   ['Revisión y publicación', 'Edita y publica las lecciones y cuestionarios generados.'],
  history:  ['Historial', 'Ejecuciones guardadas en disco.'],
};

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// ---------------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------------
async function api(path, body, timeoutMs) {
  const ctrl = new AbortController();
  const timer = timeoutMs ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
  const opt = body === undefined
    ? { signal: ctrl.signal }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: ctrl.signal };
  try {
    const r = await fetch(path, opt);
    return await r.json().catch(() => ({ ok: false, error: 'respuesta no JSON (' + r.status + ')' }));
  } catch (e) {
    if (e && e.name === 'AbortError') return { ok: false, error: 'tiempo de espera agotado (' + Math.round((timeoutMs || 0) / 1000) + 's)' };
    return { ok: false, error: String(e) };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function callTool(name, args, timeout) {
  const j = await api('/api/call', { name, arguments: args, timeout }, ((timeout || 1800) + 20) * 1000);
  if (!j.ok) throw new Error(j.error || 'fallo MCP');
  if (j.result && j.result.ok === false) throw new Error(j.result.error || 'la herramienta devolvió un error');
  return j.result;
}

let toastTimer = null;
function toast(msg, kind) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast ' + (kind || '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, kind === 'err' ? 9000 : 4200);
}

function persist() {
  const { mode, course, syl, modules, run, jobId } = S;
  localStorage.setItem('mcs.state', JSON.stringify({ mode, course, syl, modules, run, jobId }));
}

function restore() {
  try {
    const raw = localStorage.getItem('mcs.state');
    if (!raw) return;
    const st = JSON.parse(raw);
    Object.assign(S, {
      mode: st.mode || 'new',
      course: Object.assign(S.course, st.course || {}),
      syl: Object.assign(S.syl, st.syl || {}),
      modules: Array.isArray(st.modules) ? st.modules : [],
      run: Object.assign(S.run, st.run || {}),
      jobId: st.jobId || null,
    });
  } catch (e) { /* estado corrupto: seguimos con valores por defecto */ }
}

function modDefaults() {
  return { name: '', lesson_title: '', lesson_topic: '', quiz_name: '', num_questions: 5, attempts: 1, difficulty: 'Intermedio / Clínico' };
}

// ---------------------------------------------------------------------------
// Navegación
// ---------------------------------------------------------------------------
function switchTab(name) {
  $$('.tab').forEach((t) => t.classList.toggle('active', t.id === 'tab-' + name));
  $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  const [title, sub] = TABS[name] || ['', ''];
  $('#viewTitle').textContent = title;
  $('#viewSub').textContent = sub;
  if (name === 'run') renderPlan();
  if (name === 'review') { renderReview(); if (!S.job) openLatestJob(); }
  if (name === 'history') loadHistory();
  if (name === 'syllabus') renderModules();
  window.scrollTo({ top: 0 });
}

$$('.nav-item').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));
$$('[data-goto]').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.goto)));

// ---------------------------------------------------------------------------
// Salud del sistema
// ---------------------------------------------------------------------------
async function health() {
  const el = $('#health');
  el.innerHTML = '<span class="dot wait"></span><span>Comprobando MCP…</span>';
  const j = await api('/api/health');
  const busy = (j.live_jobs || []).length;
  if (j.ok && j.mcp) {
    el.innerHTML = busy
      ? '<span class="dot ok"></span><span>generando · MCP activo</span>'
      : '<span class="dot ok"></span><span>moodle-ia-mcp conectado</span>';
    el.title = j.error || '';
  } else {
    el.innerHTML = '<span class="dot err"></span><span>MCP sin respuesta</span>';
    el.title = j.error || '';
  }
}

// ---------------------------------------------------------------------------
// 1. Curso
// ---------------------------------------------------------------------------
$$('.seg').forEach((b) => b.addEventListener('click', () => {
  S.mode = b.dataset.mode;
  $$('.seg').forEach((x) => x.classList.toggle('active', x === b));
  $('#paneNew').hidden = S.mode !== 'new';
  $('#paneExisting').hidden = S.mode !== 'existing';
  renderCourseMap();
  persist();
}));

function syncCourseForm() {
  $('#cFullname').value = S.course.fullname || '';
  $('#cShortname').value = S.course.shortname || '';
  $('#cSummary').value = S.course.summary || '';
  $('#cFormat').value = S.course.format || 'topics';
  $('#cCategory').value = String(S.course.category_id || '');
  $('#cCategoryId').value = S.course.category_id || 1;
}

['cFullname', 'cShortname', 'cSummary'].forEach((id) => {
  $('#' + id).addEventListener('input', (e) => {
    const k = { cFullname: 'fullname', cShortname: 'shortname', cSummary: 'summary' }[id];
    S.course[k] = e.target.value;
    persist();
  });
});
$('#cFormat').addEventListener('change', (e) => { S.course.format = e.target.value; persist(); });
$('#cCategory').addEventListener('change', (e) => { S.course.category_id = e.target.value; persist(); });
$('#cCategoryId').addEventListener('input', (e) => { S.course.category_id = e.target.value; persist(); });

async function loadCategories() {
  const sel = $('#cCategory');
  try {
    const r = await callTool('moodle_list_categories', {}, 60);
    if (!r.ok) throw new Error(r.error || 'error');
    const cats = (r.categories || []).filter((c) => c.id !== 1 || true);
    sel.innerHTML = '<option value="1">— sin categoría específica (1) —</option>' +
      cats.map((c) => `<option value="${c.id}">${esc(c.name)} (${c.coursecount || 0} cursos)</option>`).join('');
    if (S.course.category_id) sel.value = String(S.course.category_id);
  } catch (e) {
    // El token del Web Service puede no tener core_course_get_categories:
    // degradamos a entrada manual del id de categoría.
    sel.hidden = true;
    const inp = $('#cCategoryId');
    inp.hidden = false;
    inp.value = S.course.category_id || 1;
    S.course.category_id = String(inp.value);
    toast('No se pueden listar categorías (' + e.message.slice(0, 80) + '). Introduce el id a mano.', 'err');
  }
}

async function loadCourses() {
  const sel = $('#cCourseSelect');
  try {
    const r = await callTool('moodle_list_courses', {});
    if (!r.ok) throw new Error(r.error || 'error');
    const cs = r.courses || [];
    sel.innerHTML = '<option value="">— elige un curso —</option>' +
      cs.map((c) => `<option value="${c.id}">#${c.id} · ${esc(c.fullname)}</option>`).join('');
    if (S.course.id) sel.value = String(S.course.id);
  } catch (e) {
    sel.innerHTML = '<option value="">— error al cargar cursos —</option>';
    toast('No se pudieron cargar los cursos: ' + e.message, 'err');
  }
}

$('#cCourseSelect').addEventListener('change', (e) => {
  S.course.id = e.target.value ? Number(e.target.value) : null;
  persist(); renderChips();
});

$('#btnLoadStructure').addEventListener('click', async () => {
  if (!S.course.id) { toast('Elige un curso primero', 'err'); return; }
  const out = $('#structureOut');
  out.innerHTML = '<p class="note">Cargando estructura…</p>';
  try {
    const r = await callTool('moodle_get_course_structure', { course_id: S.course.id });
    if (!r.ok) throw new Error(r.error || 'error');
    const secs = (r.sections || []).filter((s) => s.section_number > 0);
    out.innerHTML = secs.map((s) => {
      const mods = (s.modules || []).map((m) => m.modname).join(', ');
      return `<div class="srow"><span class="n">${s.section_number}</span>
        <span class="name">${esc(s.name || '(sin nombre)')}</span>
        <span class="cnt">${s.modules_count} mód.${mods ? ' · ' + esc(mods) : ''}</span></div>`;
    }).join('') || '<p class="note">El curso no tiene secciones con contenido.</p>';
    const last = secs.length ? Math.max(...secs.map((s) => s.section_number)) + 1 : 1;
    S.course.startSection = last;
    out.innerHTML += `<p class="note">Las nuevas secciones se escribirán desde la sección <b>${last}</b>.</p>`;
    renderCourseMap();
  } catch (e) {
    out.innerHTML = `<p class="note warn">${esc(e.message)}</p>`;
  }
});

function renderCourseMap() {
  const box = $('#courseMap');
  if (!S.modules.length) {
    box.innerHTML = '<p class="note">Sin módulos todavía. Genera el temario en el paso 2.</p>';
    return;
  }
  const start = Number(S.course.startSection || 1);
  box.innerHTML = S.modules.map((m, i) => `
    <div class="srow"><span class="n">${start + i}</span>
      <span class="name">${esc(m.name || 'Módulo ' + (i + 1))}</span>
      <span class="cnt">1 lección · ${m.num_questions} preguntas</span></div>`).join('');
}

function renderChips() {
  const c = $('#chipCourse');
  const isNew = S.mode === 'new';
  const label = isNew ? (S.course.fullname || 'curso nuevo') : (S.course.id ? 'curso #' + S.course.id : 'sin curso');
  c.textContent = label;
  c.className = 'chip ' + ((isNew ? S.course.fullname : S.course.id) ? 'on' : 'off');
  $('#chipModel').textContent = 'modelo: ' + S.run.model;
  const all = S.job ? Object.values(S.job.results || {}) : [];
  const pending = all.filter((r) => r && r.published === false).length;
  $('#chipDraft').textContent = all.length + ' borradores' + (pending ? ' · ' + pending + ' sin publicar' : '');
  $('#chipDraft').className = 'chip ' + (pending ? 'on' : 'off');
}

// ---------------------------------------------------------------------------
// 2. Temario
// ---------------------------------------------------------------------------
function modulesFromSyllabus(syl, qpm) {
  const out = [];
  (syl.sections || []).forEach((s, i) => {
    const lesson = (s.lessons && s.lessons[0]) || {};
    const key = Array.isArray(lesson.key_topics) ? lesson.key_topics.join(', ') : (lesson.key_topics || '');
    out.push({
      name: s.name || ('Módulo ' + (i + 1)),
      lesson_title: lesson.title || ('Lección ' + (i + 1)),
      lesson_topic: [lesson.learning_goals, key].filter(Boolean).join(' · '),
      quiz_name: s.quiz_title || ('Evaluación ' + (s.name || 'Módulo ' + (i + 1))),
      num_questions: Number(qpm) || 5,
      attempts: 1,
      difficulty: 'Intermedio / Clínico',
      summary: s.summary || '',
    });
  });
  return out;
}

$('#btnSyllabus').addEventListener('click', async () => {
  const btn = $('#btnSyllabus');
  const st = $('#sylState');
  S.syl.topic = $('#sTopic').value.trim();
  S.syl.audience = $('#sAudience').value.trim();
  S.syl.num_modules = Number($('#sModules').value) || 4;
  S.syl.outcomes = $('#sOutcomes').value.trim();
  S.syl.qpm = Number($('#sQpm').value) || 5;
  persist();
  if (!S.syl.topic) { toast('Escribe el tema general del curso', 'err'); return; }
  btn.disabled = true;
  st.textContent = 'generando con ' + $('#sModel').value + '…';
  $('#sylRaw').hidden = true;
  const t0 = Date.now();
  try {
    const r = await callTool('moodle_generate_syllabus', {
      topic: S.syl.topic,
      target_audience: S.syl.audience,
      num_modules: S.syl.num_modules,
      learning_outcomes: S.syl.outcomes,
      model_target: $('#sModel').value,
      temperature: 0.2,
      questions_per_module: S.syl.qpm,
    }, 900);
    if (!r.ok) throw new Error(r.error || 'el modelo no devolvió un temario válido');
    const syl = r.syllabus || {};
    S.modules = modulesFromSyllabus(syl, S.syl.qpm);
    if (syl.course_title && !S.course.fullname) S.course.fullname = syl.course_title;
    if (syl.course_shortname && !S.course.shortname) S.course.shortname = syl.course_shortname;
    if (syl.course_summary && !S.course.summary) S.course.summary = syl.course_summary;
    syncCourseForm();
    renderModules(); renderCourseMap(); renderChips(); persist();
    st.textContent = `${S.modules.length} módulos · ${((Date.now() - t0) / 1000).toFixed(1)}s`;
    toast('Temario generado: ' + S.modules.length + ' módulos', 'ok');
  } catch (e) {
    st.textContent = 'error';
    const raw = $('#sylRaw');
    raw.hidden = false;
    raw.textContent = e.message;
    toast('Fallo al generar el temario', 'err');
  } finally {
    btn.disabled = false;
  }
});

$('#btnImportSyllabus').addEventListener('click', () => {
  const raw = prompt('Pega el JSON del temario ({"course_title":..., "sections":[...]})');
  if (!raw) return;
  try {
    const syl = JSON.parse(raw.replace(/^```(?:json)?/i, '').replace(/```$/, '').trim());
    S.modules = modulesFromSyllabus(syl, S.syl.qpm);
    if (syl.course_title && !S.course.fullname) { S.course.fullname = syl.course_title; syncCourseForm(); }
    renderModules(); renderCourseMap(); persist();
    toast('Temario importado', 'ok');
  } catch (e) {
    toast('JSON inválido: ' + e.message, 'err');
  }
});

$('#btnAddModule').addEventListener('click', () => {
  S.modules.push(Object.assign(modDefaults(), { name: 'Módulo ' + (S.modules.length + 1) }));
  renderModules(); renderCourseMap(); persist();
});

function renderModules() {
  const box = $('#modules');
  if (!S.modules.length) {
    box.innerHTML = '<p class="note">Sin módulos. Genera el temario o añade uno manualmente.</p>';
    return;
  }
  box.innerHTML = S.modules.map((m, i) => `
    <div class="mod" data-i="${i}">
      <div class="mod-head">
        <span class="idx">${i + 1}</span>
        <input data-f="name" value="${esc(m.name)}" placeholder="Nombre de la sección">
        <div class="tools">
          <button data-act="up" title="Subir">↑</button>
          <button data-act="down" title="Bajar">↓</button>
          <button data-act="del" title="Eliminar">✕</button>
        </div>
      </div>
      <div class="mod-grid">
        <label>Título de la lección
          <input data-f="lesson_title" value="${esc(m.lesson_title)}">
        </label>
        <label>Nombre del cuestionario
          <input data-f="quiz_name" value="${esc(m.quiz_name)}">
        </label>
        <label class="full">Contenido / objetivos de la lección
          <textarea data-f="lesson_topic" rows="2">${esc(m.lesson_topic)}</textarea>
        </label>
      </div>
      <div class="mod-nums">
        <label>Preguntas
          <input data-f="num_questions" type="number" min="1" max="20" value="${m.num_questions}">
        </label>
        <label>Intentos
          <input data-f="attempts" type="number" min="1" max="10" value="${m.attempts}">
        </label>
        <label>Dificultad
          <input data-f="difficulty" value="${esc(m.difficulty)}">
        </label>
      </div>
    </div>`).join('');

  box.querySelectorAll('.mod').forEach((el) => {
    const i = Number(el.dataset.i);
    el.querySelectorAll('[data-f]').forEach((inp) => {
      inp.addEventListener('input', () => {
        const f = inp.dataset.f;
        S.modules[i][f] = (inp.type === 'number') ? Number(inp.value) : inp.value;
        if (f === 'name' || f === 'num_questions') renderCourseMap();
        persist();
      });
    });
    el.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', () => {
      const a = b.dataset.act;
      if (a === 'del') S.modules.splice(i, 1);
      if (a === 'up' && i > 0) { const t = S.modules[i - 1]; S.modules[i - 1] = S.modules[i]; S.modules[i] = t; }
      if (a === 'down' && i < S.modules.length - 1) { const t = S.modules[i + 1]; S.modules[i + 1] = S.modules[i]; S.modules[i] = t; }
      renderModules(); renderCourseMap(); persist();
    }));
  });
}

// ---------------------------------------------------------------------------
// 3. Plan y ejecución
// ---------------------------------------------------------------------------
function buildPlan() {
  const steps = [];
  const start = Number(S.course.startSection || 1);
  if (S.mode === 'new') {
    steps.push({
      kind: 'create_course', key: 'create_course',
      label: `Crear curso «${S.course.fullname || '(sin nombre)'}»`,
      fullname: S.course.fullname, shortname: S.course.shortname,
      category_id: Number(S.course.category_id || 1), summary: S.course.summary, format: S.course.format,
    });
  }
  S.modules.forEach((m, i) => {
    const sec = start + i;
    steps.push({ kind: 'section', key: `s${sec}_section`, section_num: sec, name: m.name || ('Módulo ' + (i + 1)), summary_html: m.summary || '' });
    steps.push({ kind: 'page', key: `s${sec}_page`, section_num: sec, title: m.lesson_title || m.name, topic: m.lesson_topic || m.name, publish: S.run.publish });
    steps.push({
      kind: 'quiz', key: `s${sec}_quiz`, section_num: sec, name: m.quiz_name || ('Evaluación ' + (m.name || i + 1)),
      topic: m.lesson_topic || m.name, num_questions: m.num_questions || 5, attempts: m.attempts || 1,
      difficulty: m.difficulty || 'Intermedio / Clínico', publish: S.run.publish,
    });
  });
  return steps;
}

function renderPlan() {
  const steps = buildPlan();
  $('#planCount').textContent = steps.length + ' pasos · ' + (S.run.publish ? 'publica' : 'solo borradores');
  $('#planList').innerHTML = steps.length
    ? steps.map((s) => `<li><b>${esc(s.label || s.name || s.title)}</b>${s.section_num ? ' — sección ' + s.section_num : ''}</li>`).join('')
    : '<li>Sin pasos: añade módulos en el temario.</li>';
  $('#chipModel').textContent = 'modelo: ' + S.run.model;
}

$('#rModel').addEventListener('change', (e) => { S.run.model = e.target.value; persist(); renderChips(); });
$('#rTemp').addEventListener('input', (e) => { S.run.temp = Number(e.target.value); persist(); });
$('#rMaxTokens').addEventListener('input', (e) => { S.run.max_tokens = Number(e.target.value); persist(); });
$$('input[name="pubmode"]').forEach((r) => r.addEventListener('change', () => {
  S.run.publish = r.value === 'auto';
  persist(); renderPlan();
}));

$('#btnRun').addEventListener('click', async () => {
  const steps = buildPlan();
  if (!steps.length) { toast('El plan está vacío', 'err'); return; }
  if (S.mode === 'new') {
    if (!S.course.fullname.trim() || !S.course.shortname.trim()) {
      toast('Curso nuevo: faltan nombre completo o shortname', 'err'); switchTab('course'); return;
    }
  } else if (!S.course.id) {
    toast('Elige un curso existente', 'err'); switchTab('course'); return;
  }
  $('#btnRun').disabled = true;
  $('#btnCancel').disabled = false;
  try {
    const r = await api('/api/jobs', {
      course_id: S.course.id,
      plan: steps,
      options: {
        model_target: S.run.model, temperature: S.run.temp,
        max_tokens: S.run.max_tokens, dry_run: !S.run.publish,
        title: S.course.fullname || ('Curso #' + S.course.id),
      },
    });
    if (!r.ok) throw new Error(r.error || 'no se pudo lanzar');
    S.jobId = r.id; persist(); renderChips();
    toast('Plan lanzado: ' + r.id, 'ok');
    pollJob();
  } catch (e) {
    toast(e.message, 'err');
    $('#btnRun').disabled = false;
  }
});

$('#btnCancel').addEventListener('click', async () => {
  if (!S.jobId) return;
  const r = await api('/api/jobs/' + S.jobId + '/cancel', {});
  toast(r.ok ? 'Cancelación solicitada' : (r.error || 'error'), r.ok ? '' : 'err');
});

let pollTimer = null;
async function pollJob() {
  clearTimeout(pollTimer);
  if (!S.jobId) return;
  const r = await api('/api/jobs/' + S.jobId);
  if (!r.ok) return;
  S.job = r.job;
  if (S.job.course_id) S.course.id = S.job.course_id;
  renderJob(); renderChips();
  const active = ['queued', 'running'].includes(S.job.status);
  $('#btnRun').disabled = active;
  $('#btnCancel').disabled = !active;
  if (active) pollTimer = setTimeout(pollJob, 1500);
  else {
    localStorage.setItem('mcs.job', JSON.stringify(S.job));
    if (S.job.status === 'done') toast('Plan completado', 'ok');
    if (S.job.status === 'error') toast('El plan falló: mira el registro', 'err');
    renderReview();
  }
}

function renderJob() {
  const j = S.job;
  if (!j) return;
  const total = (j.plan || []).length || 1;
  const done = (j.steps || []).filter((s) => ['ok', 'error', 'interrupted'].includes(s.status)).length;
  $('#barFill').style.width = Math.round((done / total) * 100) + '%';
  const names = { queued: 'en cola', running: 'en ejecución', done: 'completado', error: 'error',
                  interrupted: 'interrumpido · se puede reanudar', cancelled: 'cancelado' };
  $('#jobState').textContent = (j.id || '') + ' · ' + (names[j.status] || j.status);
  $('#steps').innerHTML = (j.steps || []).map((s) => `
    <div class="step ${s.status}">
      <span class="badge">${s.status}</span>
      <span class="lbl">${esc(s.label)}</span>
      <span class="sum">${esc(s.summary || s.error || '')}</span>
    </div>`).join('');
  const log = $('#log');
  log.innerHTML = (j.log || []).map((l) => {
    const cls = l.level === 'error' ? 'e' : (l.level === 'warn' ? 'w' : '');
    return `<span class="${cls}">[${l.t}] ${esc(l.msg)}</span>`;
  }).join('\n');
  log.scrollTop = log.scrollHeight;
}

// ---------------------------------------------------------------------------
// 4. Revisión
// ---------------------------------------------------------------------------
function reviewItems() {
  const j = S.job;
  if (!j || !j.results) return [];
  const items = [];
  Object.entries(j.results).forEach(([key, v]) => {
    if (!v) return;
    if (key.endsWith('_page')) items.push({ key, type: 'page', data: v });
    if (key.endsWith('_quiz')) items.push({ key, type: 'quiz', data: v });
  });
  return items.sort((a, b) => (a.data.section_num || 0) - (b.data.section_num || 0));
}

async function openLatestJob() {
  const r = await api('/api/jobs');
  if (!r.ok) return;
  const jobs = (r.jobs || []).filter((x) => x.steps_ok > 0);
  if (!jobs.length) return;
  const r2 = await api('/api/jobs/' + jobs[0].id);
  if (!r2.ok) return;
  S.job = r2.job; S.jobId = r2.job.id; persist();
  renderJob(); renderChips(); renderReview();
}

function renderReview() {
  const box = $('#reviewItems');
  const items = reviewItems();
  if (!items.length) {
    box.innerHTML = '<p class="note">No hay borradores en esta ejecución. Lanza un plan en modo «Revisar antes de publicar» o abre una ejecución anterior.</p>'
      + '<div class="row-actions"><button class="small" onclick="openLatestJob()">Abrir la última ejecución</button>'
      + '<button class="small" onclick="switchTab(\'history\')">Ver historial</button></div>';
    return;
  }
  const courseId = (S.job && S.job.course_id) || S.course.id;
  box.innerHTML = items.map(({ key, type, data }) => {
    const title = type === 'page' ? (data.title || 'Lección') : (data.quiz_name || 'Cuestionario');
    const meta = type === 'page'
      ? `${data.length || (data.html || '').length} caracteres`
      : `${data.question_count || '?'} preguntas · ${esc(data.category || '')}`;
    const content = type === 'page' ? (S.edits[key] !== undefined ? S.edits[key] : (data.html || '')) : (S.edits[key] !== undefined ? S.edits[key] : (data.xml || ''));
    const badge = data.published ? '<span class="chip on">publicado</span>'
      : (data.invalid ? '<span class="chip err">XML inválido</span>' : '<span class="chip off">borrador</span>');
    return `
      <div class="ritem" data-key="${esc(key)}" data-type="${type}">
        <header>
          <div><div class="k">sección ${data.section_num} · ${type === 'page' ? 'lección' : 'cuestionario'}</div>
            <div class="t">${esc(title)}</div></div>
          ${badge}
        </header>
        ${data.invalid ? `<p class="note err" style="margin:0 14px 8px">El modelo generó un XML no válido: ${esc(data.error || '')}. Corrige el XML en la pestaña «XML» y pulsa Publicar, o reintenta el paso.</p>` : ''}
        <div class="tabs">
          <button data-view="prev" class="on">Vista previa</button>
          <button data-view="code">${type === 'page' ? 'HTML' : 'XML'}</button>
        </div>
        <div class="body">
          <iframe data-prev src="about:blank" sandbox="allow-same-origin"></iframe>
          <textarea data-code hidden>${esc(content)}</textarea>
        </div>
        <footer>
          <span class="state">${meta}</span>
          <div class="row-actions" style="margin:0">
            ${data.url ? `<a class="chip" href="${esc(data.url)}" target="_blank" rel="noopener">abrir en Moodle</a>` : ''}
            <button class="primary small" data-pub ${!courseId ? 'disabled' : ''}>${data.published ? 'Republicar' : 'Publicar'}</button>
          </div>
        </footer>
      </div>`;
  }).join('');

  box.querySelectorAll('.ritem').forEach((el) => {
    const key = el.dataset.key;
    const type = el.dataset.type;
    const frame = el.querySelector('[data-prev]');
    const ta = el.querySelector('[data-code]');
    const paint = () => {
      const html = ta.value;
      frame.srcdoc = type === 'page'
        ? `<!DOCTYPE html><html><head><meta charset="utf-8"><style>body{font:14px/1.6 -apple-system,Segoe UI,sans-serif;padding:14px;color:#16202c;max-width:820px}h1,h2,h3{color:#123}</style></head><body>${html}</body></html>`
        : `<pre style="font:11px/1.5 monospace;white-space:pre-wrap;padding:10px">${esc(html)}</pre>`;
    };
    paint();
    el.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => {
      const prev = b.dataset.view === 'prev';
      el.querySelectorAll('.tabs button').forEach((x) => x.classList.toggle('on', x === b));
      frame.hidden = !prev;
      ta.hidden = prev;
    }));
    ta.addEventListener('input', () => { S.edits[key] = ta.value; paint(); });
    el.querySelector('[data-pub]').addEventListener('click', (e) => publishItem(key, type, ta.value, e.target));
  });
}

async function publishItem(key, type, content, btn) {
  const courseId = (S.job && S.job.course_id) || S.course.id;
  const data = (S.job.results || {})[key] || {};
  if (!courseId) { toast('No hay course_id', 'err'); return; }
  btn.disabled = true;
  btn.textContent = 'publicando…';
  try {
    let patch = { published: true };
    if (type === 'page') {
      const r = await callTool('moodle_add_page_content', {
        course_id: courseId, section_num: data.section_num,
        name: data.title || 'Lección', content_html: content,
      }, 600);
      if (!r.ok) throw new Error(r.error || 'error al publicar la página');
      const cmid = (r.result || {}).coursemoduleid || (r.result || {}).cmid;
      if (cmid) patch.coursemoduleid = cmid;
    } else {
      const r = await callTool('moodle_generate_and_import_quiz', {
        course_id: courseId, section_num: data.section_num,
        quiz_name: data.quiz_name || 'Evaluación', topic: data.quiz_name || 'Evaluación',
        num_questions: data.question_count || 5, category_name: data.category || '',
        xml_content: content, attempts: data.attempts || 1, dry_run: false,
      }, 900);
      if (!r.ok) throw new Error(r.error || 'error al importar el cuestionario');
      patch.quiz_instance_id = r.quiz_instance_id;
    }
    await api('/api/jobs/' + S.job.id + '/results', { key, patch });
    Object.assign(data, patch);
    toast('Publicado: ' + (data.title || data.quiz_name || key), 'ok');
    renderReview(); renderChips();
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Publicar';
  }
}

$('#btnPublishAll').addEventListener('click', async () => {
  const items = reviewItems().filter((i) => !i.data.published);
  if (!items.length) { toast('No hay borradores pendientes'); return; }
  if (!confirm(`¿Publicar ${items.length} elementos en Moodle?`)) return;
  for (const it of items) {
    const el = document.querySelector(`.ritem[data-key="${it.key}"]`);
    const ta = el && el.querySelector('[data-code]');
    const btn = el && el.querySelector('[data-pub]');
    if (btn) await publishItem(it.key, it.type, ta ? ta.value : '', btn);
  }
});

$('#btnRetryFailed').addEventListener('click', async () => {
  if (!S.job) { toast('No hay ejecución abierta', 'err'); return; }
  const doneKeys = new Set((S.job.steps || []).filter((s) => s.status === 'ok').map((s) => s.key));
  const pend = (S.job.plan || []).map((p) => p.key).filter((k) => !doneKeys.has(k));
  if (!pend.length) { toast('La ejecución ya tiene todos los pasos completados'); return; }
  if (!confirm(`¿Reanudar ${pend.length} paso(s) pendiente(s) desde el último completado?\n\n${pend.join(', ')}`)) return;
  const r = await api('/api/jobs/' + S.job.id + '/retry', { keys: pend }, 90000);
  if (!r.ok) { toast(r.error || 'no se pudo reanudar', 'err'); return; }
  toast('Reanudando: ' + pend.join(', '), 'ok');
  pollJob();
});

$('#btnRefreshJob').addEventListener('click', () => { if (S.jobId) pollJob(); });

// ---------------------------------------------------------------------------
// 5. Historial
// ---------------------------------------------------------------------------
async function loadHistory() {
  const box = $('#history');
  box.innerHTML = '<p class="note">Cargando…</p>';
  const r = await api('/api/jobs');
  if (!r.ok) { box.innerHTML = '<p class="note warn">No se pudo leer el historial</p>'; return; }
  const jobs = r.jobs || [];
  if (!jobs.length) { box.innerHTML = '<p class="note">Sin ejecuciones registradas.</p>'; return; }
  box.innerHTML = jobs.map((j) => `
    <div class="hrow" data-id="${esc(j.id)}">
      <span>${esc(j.id)}</span>
      <span class="st ${j.status}">${j.status}</span>
      <span>${esc(j.title || ('curso #' + (j.course_id || '—')))}</span>
      <span>${j.steps_ok}/${j.steps_total}</span>
      <span>${j.duration}s</span>
    </div>`).join('');
  box.querySelectorAll('.hrow').forEach((row) => row.addEventListener('click', async () => {
    const r2 = await api('/api/jobs/' + row.dataset.id);
    if (!r2.ok) return;
    S.job = r2.job; S.jobId = r2.job.id; persist();
    renderJob(); renderChips(); switchTab('review');
  }));
}

$('#btnHistRefresh').addEventListener('click', loadHistory);

// ---------------------------------------------------------------------------
// Global
// ---------------------------------------------------------------------------
$('#themeBtn').addEventListener('click', () => {
  const cur = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = cur;
  $('#themeBtn').textContent = cur === 'light' ? 'Tema oscuro' : 'Tema claro';
  localStorage.setItem('mcs.theme', cur);
});

$('#resetBtn').addEventListener('click', () => {
  if (!confirm('¿Borrar el formulario y los módulos? El historial se conserva.')) return;
  S.mode = 'new';
  S.course = { id: null, fullname: '', shortname: '', category_id: '', summary: '', format: 'topics', startSection: 1 };
  S.modules = []; S.edits = {}; S.jobId = null; S.job = null;
  localStorage.removeItem('mcs.state');
  syncCourseForm(); renderModules(); renderCourseMap(); renderChips(); renderPlan();
  $('#steps').innerHTML = ''; $('#log').textContent = ''; $('#barFill').style.width = '0%';
  switchTab('course');
});

(function boot() {
  const theme = localStorage.getItem('mcs.theme') || 'dark';
  document.documentElement.dataset.theme = theme;
  $('#themeBtn').textContent = theme === 'light' ? 'Tema oscuro' : 'Tema claro';

  restore();
  syncCourseForm();
  $$('.seg').forEach((x) => x.classList.toggle('active', x.dataset.mode === S.mode));
  $('#paneNew').hidden = S.mode !== 'new';
  $('#paneExisting').hidden = S.mode !== 'existing';
  $('#sTopic').value = S.syl.topic; $('#sAudience').value = S.syl.audience;
  $('#sModules').value = S.syl.num_modules; $('#sOutcomes').value = S.syl.outcomes; $('#sQpm').value = S.syl.qpm;
  $('#rModel').value = S.run.model; $('#rTemp').value = S.run.temp; $('#rMaxTokens').value = S.run.max_tokens;
  $$('input[name="pubmode"]').forEach((r) => { r.checked = (r.value === 'auto') === !!S.run.publish; });
  renderModules(); renderCourseMap(); renderChips(); renderPlan();
  health(); loadCategories(); loadCourses();
  if (S.jobId) pollJob();
})();
