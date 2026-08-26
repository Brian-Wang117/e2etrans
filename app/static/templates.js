// Campaign-template management page: list / create / edit / delete outbound
// templates (company profile, opening, fixed scripts, persona). Validation
// mirrors app/outbound/script_library.validate_script so bad input fails fast
// before hitting the server.

const VERDICT_OPTIONS = [
  { value: "", label: "不定结论" },
  { value: "感兴趣", label: "感兴趣" },
  { value: "不感兴趣", label: "不感兴趣" },
  { value: "中立", label: "中立" },
];
const MAX_BOT_NAME_CHARS = 20;
const MIN_REPLY_CHARS = 20;
const MAX_REPLY_CHARS = 40;
const PRICE_PATTERN = /(¥|￥|\$|\d+\s*(元|块钱|块|折))/;

const listBody = document.getElementById("template-body");
const editorPanel = document.getElementById("editor-panel");
const editorTitle = document.getElementById("editor-title");
const scriptBody = document.getElementById("script-body");
const scriptCount = document.getElementById("script-count");
const editorError = document.getElementById("editor-error");
const editorStatus = document.getElementById("editor-status");

let templates = [];
let editingId = null; // null = creating

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    /* non-JSON error bodies */
  }
  if (!response.ok) {
    const detail = body?.detail;
    const message = Array.isArray(detail?.errors)
      ? detail.errors.join("\n")
      : typeof detail === "string"
        ? detail
        : `请求失败（${response.status}）`;
    throw new Error(message);
  }
  return body;
}

async function refresh() {
  try {
    const data = await api("/api/templates");
    templates = data.templates || [];
    renderList();
  } catch (error) {
    editorError.textContent = error.message;
  }
}

function renderList() {
  listBody.replaceChildren();
  for (const template of templates) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    nameCell.textContent = template.name;
    const companyCell = document.createElement("td");
    companyCell.textContent = template.company_name || "—";
    const defaultCell = document.createElement("td");
    if (template.is_default) {
      const badge = document.createElement("span");
      badge.className = "tp-default-badge";
      badge.textContent = "默认";
      defaultCell.appendChild(badge);
    }
    const countCell = document.createElement("td");
    countCell.textContent = String((template.scripts || []).length);
    const actionCell = document.createElement("td");
    actionCell.className = "tp-actions";
    actionCell.appendChild(button("编辑", () => openEditor(template)));
    if (!template.is_default) {
      actionCell.appendChild(
        button("设为默认", () => setDefault(template)),
      );
    }
    actionCell.appendChild(
      button("删除", () => removeTemplate(template), "danger"),
    );
    row.append(nameCell, companyCell, defaultCell, countCell, actionCell);
    listBody.appendChild(row);
  }
}

function button(text, onClick, className = "") {
  const element = document.createElement("button");
  element.type = "button";
  element.textContent = text;
  if (className) element.className = className;
  element.addEventListener("click", onClick);
  return element;
}

// -- editor ------------------------------------------------------------------

function openEditor(template = null) {
  editingId = template ? template.id : null;
  editorTitle.textContent = template ? `编辑模板：${template.name}` : "新建模板";
  setValue("f-name", template?.name || "");
  setValue("f-company", template?.company_name || "");
  setValue("f-background", template?.business_background || "");
  setValue("f-opening", template?.opening_template || "");
  setValue("f-botname", template?.bot_name || "");
  setValue("f-style", template?.speaking_style || "");
  document.getElementById("f-default").checked = Boolean(template?.is_default);
  scriptBody.replaceChildren();
  for (const script of template?.scripts || []) appendScriptRow(script);
  clearMessages();
  editorPanel.hidden = false;
  editorPanel.scrollIntoView({ behavior: "smooth" });
}

function closeEditor() {
  editorPanel.hidden = true;
  editingId = null;
  clearMessages();
}

function setValue(id, value) {
  document.getElementById(id).value = value;
}

function clearMessages() {
  editorError.textContent = "";
  editorStatus.textContent = "";
}

function appendScriptRow(script = {}) {
  const row = document.createElement("tr");
  const categoryCell = document.createElement("td");
  const category = input("text", script.category || "");
  category.placeholder = "如：询价";
  categoryCell.appendChild(category);

  const triggerCell = document.createElement("td");
  const triggers = input("text", (script.triggers || []).join("，"));
  triggers.placeholder = "多少钱，价格";
  triggerCell.appendChild(triggers);

  const replyCell = document.createElement("td");
  const reply = document.createElement("textarea");
  reply.value = script.reply || "";
  reply.placeholder = `${MIN_REPLY_CHARS}-${MAX_REPLY_CHARS} 字的固定回复`;
  replyCell.appendChild(reply);

  const endCallCell = document.createElement("td");
  const endCall = document.createElement("input");
  endCall.type = "checkbox";
  endCall.checked = Boolean(script.end_call);
  endCallCell.appendChild(endCall);

  const verdictCell = document.createElement("td");
  const verdict = document.createElement("select");
  for (const option of VERDICT_OPTIONS) {
    const element = document.createElement("option");
    element.value = option.value;
    element.textContent = option.label;
    if (option.value === (script.verdict || "")) element.selected = true;
    verdict.appendChild(element);
  }
  verdictCell.appendChild(verdict);

  const priorityCell = document.createElement("td");
  const priority = input("number", String(script.priority ?? 5));
  priority.min = "0";
  priority.max = "10";
  priorityCell.appendChild(priority);

  const removeCell = document.createElement("td");
  removeCell.appendChild(button("移除", () => {
    row.remove();
    updateScriptCount();
  }));

  row.append(
    categoryCell,
    triggerCell,
    replyCell,
    endCallCell,
    verdictCell,
    priorityCell,
    removeCell,
  );
  scriptBody.appendChild(row);
  updateScriptCount();
  return row;
}

function input(type, value) {
  const element = document.createElement("input");
  element.type = type;
  element.value = value;
  return element;
}

function updateScriptCount() {
  scriptCount.textContent = `（${scriptBody.children.length} 条）`;
}

function collectScripts() {
  const scripts = [];
  for (const row of scriptBody.children) {
    const [category, triggers, reply, endCall, verdict, priority] = row.cells;
    scripts.push({
      row,
      category: category.querySelector("input").value.trim(),
      triggers: triggers
        .querySelector("input")
        .value.split(/[,，]/)
        .map((trigger) => trigger.trim())
        .filter(Boolean),
      reply: reply.querySelector("textarea").value.trim(),
      end_call: endCall.querySelector("input").checked,
      verdict: verdict.querySelector("select").value,
      priority: Number(priority.querySelector("input").value),
    });
  }
  return scripts;
}

// Mirrors validate_script in app/outbound/script_library.py.
function validateScripts(scripts) {
  const errors = [];
  scripts.forEach((script, index) => {
    const problems = [];
    if (!script.category) problems.push("类别为空");
    if (!script.triggers.length) problems.push("触发词为空");
    for (const trigger of script.triggers) {
      if (PRICE_PATTERN.test(trigger)) problems.push("触发词写死了价格");
    }
    if (script.reply.length < MIN_REPLY_CHARS || script.reply.length > MAX_REPLY_CHARS) {
      problems.push(`回复长度 ${script.reply.length} 超出 ${MIN_REPLY_CHARS}-${MAX_REPLY_CHARS}`);
    }
    if (script.end_call && !script.reply.includes("再见")) {
      problems.push("结束通话的回复必须包含“再见”");
    }
    if (PRICE_PATTERN.test(script.reply)) problems.push("回复写死了价格");
    if (!Number.isInteger(script.priority) || script.priority < 0 || script.priority > 10) {
      problems.push("优先级必须是 0-10 的整数");
    }
    if (problems.length) {
      script.row.classList.add("row-error");
      errors.push(`第 ${index + 1} 条话术：${problems.join("；")}`);
    } else {
      script.row.classList.remove("row-error");
    }
  });
  return errors;
}

async function save() {
  clearMessages();
  const errors = [];
  const name = document.getElementById("f-name").value.trim();
  const companyName = document.getElementById("f-company").value.trim();
  const botName = document.getElementById("f-botname").value.trim();
  if (!name) errors.push("模板名不能为空");
  if (!companyName) errors.push("公司名称不能为空");
  if (botName.length > MAX_BOT_NAME_CHARS) {
    errors.push(`AI 名字不能超过 ${MAX_BOT_NAME_CHARS} 字`);
  }
  const scripts = collectScripts();
  errors.push(...validateScripts(scripts));
  if (errors.length) {
    editorError.textContent = errors.join("\n");
    return;
  }
  const payload = {
    name,
    company_name: companyName,
    business_background: document.getElementById("f-background").value.trim(),
    opening_template: document.getElementById("f-opening").value.trim(),
    bot_name: botName,
    speaking_style: document.getElementById("f-style").value.trim(),
    is_default: document.getElementById("f-default").checked,
    scripts: scripts.map(({ row, ...script }) => script),
  };
  try {
    if (editingId == null) {
      await api("/api/templates", { method: "POST", body: JSON.stringify(payload) });
      editorStatus.textContent = "模板已创建";
    } else {
      await api(`/api/templates/${editingId}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      editorStatus.textContent = "模板已保存";
      if (payload.is_default) {
        await api(`/api/templates/${editingId}/default`, { method: "POST" });
      }
    }
    await refresh();
  } catch (error) {
    editorError.textContent = error.message;
  }
}

async function setDefault(template) {
  clearMessages();
  try {
    await api(`/api/templates/${template.id}/default`, { method: "POST" });
    await refresh();
  } catch (error) {
    editorError.textContent = error.message;
  }
}

async function removeTemplate(template) {
  if (!window.confirm(`确定删除模板「${template.name}」？已确认批次保留其名称快照。`)) {
    return;
  }
  clearMessages();
  try {
    await api(`/api/templates/${template.id}`, { method: "DELETE" });
    if (editingId === template.id) closeEditor();
    await refresh();
  } catch (error) {
    editorError.textContent = error.message;
  }
}

document.getElementById("new-btn").addEventListener("click", () => openEditor());
document.getElementById("add-script-btn").addEventListener("click", () => appendScriptRow());
document.getElementById("save-btn").addEventListener("click", save);
document.getElementById("cancel-btn").addEventListener("click", closeEditor);

refresh();
