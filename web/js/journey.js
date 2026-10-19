// 工作台与发票库共用的行程字段编辑器；完整度真值来自后端。
const JOURNEY_KINDS = new Set(['train', 'air', 'taxi', 'bus', 'road', 'transit', 'hotel']);
const journeyChannel = typeof BroadcastChannel === 'function'
  ? new BroadcastChannel('fapiao-journey-updates') : null;

if (journeyChannel) journeyChannel.onmessage = (event) => {
  const saved = event.data?.row;
  if (!saved || !Number.isInteger(Number(saved.id))) return;
  if (typeof vault !== 'undefined') {
    const row = vault.rows.find((item) => Number(item.id) === Number(saved.id));
    if (row) {
      Object.assign(row, saved);
      if (state.mod === 'vault') vaultRenderTable();
      const drawer = document.getElementById('vaultJourneyDrawer');
      if (drawer && Number(drawer.dataset.id) === Number(saved.id)) vaultOpenDetail(row);
    }
  }
  if (typeof wb !== 'undefined') {
    const row = wb.rows.find((item) => Number(item.vault_id) === Number(saved.id));
    if (row) wbApplySavedJourney(row, saved);
  }
};

function journeyStatusHtml(row) {
  if (!JOURNEY_KINDS.has(String(row.kind || ''))) return '<span class="muted">—</span>';
  const missing = Array.isArray(row.missing_info) ? row.missing_info : [];
  const incomplete = row.completeness !== 'complete';
  const title = incomplete ? `缺少：${missing.join('、') || '行程信息'}` : '行程信息完整';
  return `<span class="info-state ${incomplete ? 'missing' : 'complete'}" title="${esc(title)}">`
    + `${incomplete ? '信息待补充' : '信息完整'}</span>`;
}

let airportCatalogPromise;
function journeyAirportCatalog() {
  if (!airportCatalogPromise) airportCatalogPromise = fetch('/api/pool/airports')
    .then((response) => {
      if (!response.ok) throw new Error('机场目录读取失败');
      return response.json();
    });
  return airportCatalogPromise;
}

function journeyAirportFields(label, key, value, airport) {
  return `<div class="journey-airport" data-air-key="${key}">`
    + `<span>${label}</span><select data-air-province aria-label="${label}省份"><option value="">选择省份</option></select>`
    + `<select data-air-city aria-label="${label}城市"><option value="">选择城市</option></select>`
    + `<select data-air-airport aria-label="${label}机场"><option value="">机场可选</option></select>`
    + `<input type="hidden" data-jfield="${key}" value="${esc(value)}">`
    + `<input type="hidden" data-jfield="${key === 'from_' ? 'from_airport' : 'to_airport'}" value="${esc(airport)}">`
    + `</div>`;
}

async function journeyAttachAirports(root) {
  const groups = [...root.querySelectorAll('.journey-airport')];
  if (!groups.length) return;
  const data = await journeyAirportCatalog();
  const provinces = data.provinces || {};
  const options = (values, first) => `<option value="">${first}</option>`
    + values.map((value) => `<option value="${esc(value)}">${esc(value)}</option>`).join('');
  for (const group of groups) {
    const province = group.querySelector('[data-air-province]');
    const city = group.querySelector('[data-air-city]');
    const airport = group.querySelector('[data-air-airport]');
    const cityValue = group.querySelector('[data-jfield="' + group.dataset.airKey + '"]');
    const airportValue = group.querySelector('[data-jfield="' + (group.dataset.airKey === 'from_' ? 'from_airport' : 'to_airport') + '"]');
    const oldCity = cityValue.value;
    const found = Object.entries(provinces).find(([, cities]) => Object.hasOwn(cities, oldCity));
    province.innerHTML = options(Object.keys(provinces), '选择省份');
    if (found) province.value = found[0];
    const paintCities = () => {
      city.innerHTML = options(Object.keys(provinces[province.value] || {}), '选择城市');
      airport.innerHTML = options([], '机场可选');
      cityValue.value = airportValue.value = '';
    };
    const paintAirports = () => {
      airport.innerHTML = options(provinces[province.value]?.[city.value] || [], '机场可选');
      cityValue.value = city.value;
      airportValue.value = '';
    };
    province.addEventListener('change', paintCities);
    city.addEventListener('change', paintAirports);
    airport.addEventListener('change', () => { airportValue.value = airport.value; });
    paintCities();
    if (found) {
      city.value = oldCity;
      paintAirports();
      airport.value = airportValue.value = [...airport.options].some((item) => item.value === airportValue.defaultValue)
        ? airportValue.defaultValue : '';
    }
  }
}

function journeyEditorHtml(row, legacy = {}) {
  const kind = String(row.kind || '');
  if (!JOURNEY_KINDS.has(kind)) return '';
  const j = row.journey || {};
  const val = (key, fallback = '') => esc(String(j[key] || legacy[fallback] || ''));
  const field = (label, key, type = 'text', placeholder = '', fallback = '') => {
    const date = type === 'date';
    return `<label>${label}<input data-jfield="${key}" type="text" value="${val(key, fallback)}" `
      + `${date ? 'class="journey-date" readonly inputmode="none" ' : ''}`
      + `placeholder="${esc(date ? 'YYYY-MM-DD' : placeholder)}" autocomplete="off"></label>`;
  };
  let fields = '';
  if (kind === 'hotel') {
    fields = field('入住日期', 'checkin', 'date') + field('退房日期', 'checkout', 'date');
  } else {
    fields = (kind === 'air'
      ? journeyAirportFields('出发地', 'from_', j.from_, j.from_airport)
        + journeyAirportFields('目的地', 'to_', j.to_, j.to_airport)
      : field('出发地', 'from_', 'text', '填写出发地', 'from_')
        + field('目的地', 'to_', 'text', '填写目的地', 'to_'))
      + field('行程日期', 'travel_date', 'date', '', 'date');
    if (kind === 'air' || kind === 'train') {
      fields += field('乘客姓名', 'passenger', 'text', '填写乘客姓名');
    }
    if (kind === 'air' || kind === 'train') {
      fields += field(kind === 'air' ? '航班号' : '车次', 'train', 'text', '选填', 'no');
    }
  }
  const note = kind === 'hotel'
    ? '未识别到住宿日期时，退房日期默认采用开票日期；可直接修改。购买方抬头不是入住人。'
    : '补充的行程信息会保存到发票库，并用于之后生成的行程单。';
  const nights = kind === 'hotel' && j.nights ? `<span class="journey-nights">${j.nights} 晚</span>` : '';
  return `<section class="detail-sec trip-ov journey-editor"><h4>行程信息 ${journeyStatusHtml(row)}</h4>`
    + `<p class="detail-note">${note}</p><div class="ov-grid">${fields}</div>`
    + `<div class="ov-actions"><button type="button" class="btn-mini primary" data-journey-save>保存</button>`
    + `${nights}</div></section>`;
}

function journeyFormUpdates(form) {
  const updates = {};
  form.querySelectorAll('[data-jfield]').forEach((input) => {
    updates[input.dataset.jfield] = input.value.trim();
  });
  return updates;
}

async function journeySave(row, form) {
  const identity = Number(row.vault_id || (row.stored_path ? row.id : 0));
  const payload = {
    ...(Number.isInteger(identity) && identity > 0 ? { id: identity } : { path: row.path }),
    updates: journeyFormUpdates(form),
  };
  const { data } = await api('/api/vault/journey', {
    method: 'POST', body: JSON.stringify(payload),
  });
  if (!data || data.ok !== true) throw new Error(data?.error || '保存失败，请稍后重试');
  if (journeyChannel) journeyChannel.postMessage({ row: data.row });
  return data.row;
}
