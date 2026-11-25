for (const button of document.querySelectorAll('[data-copy-url]')) {
  const input = button.parentElement.querySelector('input');
  const status = button.parentElement.nextElementSibling;
  button.hidden = false;
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(input.value);
      status.textContent = '已复制订阅 URL。';
    } catch {
      input.focus();
      input.select();
      input.setSelectionRange(0, input.value.length);
      status.textContent = document.execCommand('copy') ? '已复制订阅 URL。' : '已选中链接，请手动复制。';
    }
  });
}

const visualEditor = document.querySelector('#visual-rules');
if (visualEditor) initRulesEditor(visualEditor);

if (document.querySelector('[data-update-poll]')) {
  const refresh = async () => {
    try {
      const page = await fetch(location.pathname, { redirect: 'manual', signal: AbortSignal.timeout(5000) });
      if (page.type === 'opaqueredirect' || (page.ok && !(await page.text()).includes('data-update-poll'))) {
        location.replace(location.pathname);
        return;
      }
    } catch {
      // The admin server may be restarting during an upgrade.
    }
    setTimeout(refresh, 3500);
  };
  setTimeout(refresh, 1500);
}

function initRulesEditor(editor) {
  const yamlPanel = document.querySelector('#rule-yaml');
  const yaml = yamlPanel.querySelector('textarea');
  const mode = document.querySelector('#rule-edit-mode');
  const pending = document.querySelector('#yaml-pending');
  const { rules, commaPayload, yamlPending } = JSON.parse(editor.querySelector('#rule-editor-data').textContent);
  const kind = editor.querySelector('#rule-type');
  const value = editor.querySelector('#rule-value');
  const policy = editor.querySelector('#rule-policy');
  const options = editor.querySelector('#rule-options');
  const apply = editor.querySelector('#apply-rule');
  const cancel = editor.querySelector('#cancel-rule');
  const list = editor.querySelector('#rule-list');
  const feedback = editor.querySelector('#rule-feedback');
  const placeholders = {
    DOMAIN: '例如 www.example.com', 'DOMAIN-SUFFIX': '例如 example.com',
    'DOMAIN-KEYWORD': '例如 example', 'IP-CIDR': '例如 192.0.2.0/24',
    'IP-CIDR6': '例如 2001:db8::/32', GEOIP: '例如 CN', GEOSITE: '例如 cn',
  };
  let editing = -1;
  let generatedYaml = yaml.value;
  let generatedMode = mode.value;

  function splitRule(rule) {
    const [type, ...parts] = rule.split(',');
    if (type === 'MATCH') return [type, '', parts[0], ''];
    if (commaPayload.includes(type)) return [type, parts.slice(0, -1).join(','), parts.at(-1), ''];
    return [type, parts[0], parts[1], parts.slice(2).join(',')];
  }

  function updateFields() {
    value.parentElement.hidden = kind.value === 'MATCH';
    options.parentElement.hidden = kind.value === 'MATCH' || commaPayload.includes(kind.value);
    value.placeholder = placeholders[kind.value] || '';
  }

  function resetForm() {
    editing = -1;
    value.value = '';
    options.value = '';
    apply.textContent = '添加规则';
    cancel.hidden = true;
    updateFields();
  }

  function syncMode() {
    const hasPendingYaml = yaml.value !== generatedYaml || yamlPending;
    mode.value = hasPendingYaml ? 'yaml' : generatedMode;
    editor.hidden = hasPendingYaml;
    pending.hidden = !hasPendingYaml;
  }

  function render() {
    list.replaceChildren();
    if (!rules.length) {
      const empty = document.createElement('li');
      empty.textContent = '保存后，未匹配的流量默认走代理。';
      list.append(empty);
    }
    for (const [index, rule] of rules.entries()) {
      const row = document.createElement('li');
      row.className = 'rule-row';
      const content = document.createElement('div');
      const type = document.createElement('strong');
      const detail = document.createElement('span');
      const [matchType, matchValue, target, flags] = splitRule(rule);
      type.textContent = [...kind.options].find((option) => option.value === matchType)?.textContent ?? matchType;
      detail.textContent = [matchValue, target, flags].filter(Boolean).join(' · ');
      content.append(type, detail);
      const actions = document.createElement('div');
      actions.className = 'rule-row-actions';
      function action(label, handler, disabled = false) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'secondary';
        button.textContent = label;
        button.setAttribute('aria-label', `${label}第 ${index + 1} 条规则`);
        button.disabled = disabled;
        button.addEventListener('click', handler);
        actions.append(button);
      }
      function move(offset) {
        const next = index + offset;
        [rules[index], rules[next]] = [rules[next], rules[index]];
        commit('已调整顺序，请保存规则。');
        list.children[next].querySelector('button').focus();
      }
      action('编辑', () => {
        editing = index;
        [kind.value, value.value, policy.value, options.value] = splitRule(rule);
        apply.textContent = '应用修改';
        cancel.hidden = false;
        updateFields();
        kind.focus();
      });
      action('上移', () => move(-1), index === 0);
      action('下移', () => move(1), index === rules.length - 1);
      action('删除', () => {
        rules.splice(index, 1);
        commit('已删除，请保存规则。');
        const next = list.children[Math.min(index, rules.length - 1)];
        (next?.querySelector('button') ?? kind).focus();
      });
      row.append(content, actions);
      list.append(row);
    }
  }

  function commit(message) {
    const quoteYaml = (rule) => JSON.stringify(rule).replace(/[\u0085\u2028\u2029]/g, (char) =>
      `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`);
    const nextYaml = rules.length ? `rules:\n${rules.map((rule) => `  - ${quoteYaml(rule)}\n`).join('')}` : '';
    generatedYaml = nextYaml;
    generatedMode = 'visual';
    yaml.value = nextYaml;
    syncMode();
    feedback.textContent = message;
    resetForm();
    render();
  }

  kind.addEventListener('change', updateFields);
  for (const field of [value, options]) {
    field.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.isComposing) {
        event.preventDefault();
        apply.click();
      }
    });
  }
  cancel.addEventListener('click', () => { resetForm(); kind.focus(); });
  yaml.addEventListener('input', syncMode);
  apply.addEventListener('click', () => {
    const type = kind.value;
    const matcher = value.value.trim();
    const flags = options.parentElement.hidden ? '' : options.value.trim();
    if (type !== 'MATCH' && (!matcher || (!commaPayload.includes(type) && matcher.includes(',')))) {
      feedback.textContent = '请填写有效的匹配值。';
      value.focus();
      return;
    }
    const rule = type === 'MATCH' ? `${type},${policy.value}` : [type, matcher, policy.value, flags].filter(Boolean).join(',');
    if (editing >= 0) {
      rules[editing] = rule;
    } else {
      const fallback = rules.findIndex((entry) => entry.startsWith('MATCH,'));
      if (type === 'MATCH' && fallback >= 0) {
        rules[fallback] = rule;
      } else {
        rules.splice(fallback < 0 ? rules.length : fallback, 0, rule);
      }
    }
    commit('规则已更新，请保存规则。');
  });

  updateFields();
  render();
  syncMode();
  yamlPanel.open = yamlPending;
}
