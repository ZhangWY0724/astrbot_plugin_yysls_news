const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const setNotice = (node, text, error = false) => {
  node.textContent = text || "";
  node.classList.toggle("error", Boolean(error));
};

const api = async (method, endpoint, data) => {
  if (method === "GET") return bridge.apiGet(endpoint);
  return bridge.apiPost(endpoint, data || {});
};

const element = (tag, text, className = "") => {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
};

await bridge.ready();

async function loadState() {
  try {
    const data = await api("GET", "status");
    $("enabled").textContent = data.enabled ? "运行中" : "已停用";
    $("credential").textContent = data.credential.status;
    $("account").textContent = data.credential.username || (data.credential.status === "logged_in" ? "已登录，名称暂不可用" : "暂无已登录账号");
    $("subscriptions").textContent = String(data.subscriptions.length);
    $("target-count").textContent = String(data.targets.length);
    $("queue").textContent = `待推送 ${data.queue.pending || 0} · 重试 ${data.queue.retry || 0} · 失败 ${data.queue.failed || 0}`;
    renderTargets(data.targets);
    await loadFailures();
  } catch (error) {
    setNotice($("queue"), error.message || "读取状态失败", true);
  }
}

function renderTargets(targets) {
  const root = $("targets");
  root.replaceChildren();
  if (!targets.length) {
    root.append(element("p", "尚无推送目标。"));
    return;
  }
  for (const target of targets) {
    const row = element("div", "", "item");
    const label = element(
      "div",
      `${target.display_name || "会话"} · ${target.unified_msg_origin}`,
      "mono",
    );
    const remove = element("button", "删除", "danger");
    remove.addEventListener("click", async () => {
      remove.disabled = true;
      try {
        await api("POST", `targets/${target.id}/delete`, {});
        await loadState();
      } catch (error) {
        setNotice($("target-message"), error.message || "删除失败", true);
        remove.disabled = false;
      }
    });
    row.append(label, remove);
    root.append(row);
  }
}

async function loadFailures() {
  const data = await api("GET", "failures");
  const root = $("failures");
  root.replaceChildren();
  if (!data.failures.length) {
    root.append(element("p", "暂无失败记录。"));
    return;
  }
  for (const failure of data.failures) {
    root.append(
      element(
        "div",
        `${failure.title || "资讯更新"} · 第 ${failure.attempt_number} 次 · ${failure.error || "推送失败"}`,
        "item",
      ),
    );
  }
}

$("refresh").addEventListener("click", loadState);
$("target-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await api("POST", "targets/add", {
      unified_msg_origin: $("umo").value.trim(),
      display_name: $("target-name").value.trim(),
    });
    $("umo").value = "";
    $("target-name").value = "";
    setNotice($("target-message"), "推送目标已添加。");
    await loadState();
  } catch (error) {
    setNotice($("target-message"), error.message || "添加失败", true);
  } finally {
    button.disabled = false;
  }
});

$("test-push-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("test-push-button");
  button.disabled = true;
  setNotice($("test-push-message"), "正在读取动态并推送到已启用目标…");
  try {
    const result = await api("POST", "test-push", {
      dynamic_id: $("dynamic-id").value.trim(),
    });
    const counts = result.delivery || {};
    setNotice(
      $("test-push-message"),
      `${result.author || "B 站用户"} · ${result.title || `动态 ${result.dynamic_id}`}；` +
        `目标 ${result.target_count} 个：成功 ${counts.sent || 0}，` +
        `重试 ${counts.retry || 0}，失败 ${counts.failed || 0}，待处理 ${counts.pending || 0}。`,
    );
    await loadState();
  } catch (error) {
    setNotice($("test-push-message"), error.message || "测试推送失败", true);
  } finally {
    button.disabled = false;
  }
});

$("start-login").addEventListener("click", async () => {
  const button = $("start-login");
  button.disabled = true;
  try {
    const state = await api("POST", "login/start", {});
    $("qr").src = state.image_data_url || "";
    $("qr").style.display = state.image_data_url ? "block" : "none";
    setNotice($("login-message"), state.message || "等待扫码。");
  } catch (error) {
    setNotice($("login-message"), error.message || "生成二维码失败", true);
    button.disabled = false;
  }
});

async function pollLogin() {
  try {
    const state = await api("GET", "login/status");
    setNotice($("login-message"), state.message || "");
    if (state.status !== "waiting" && state.status !== "not_started") {
      $("qr").style.display = "none";
      $("start-login").disabled = false;
      await loadState();
    }
  } catch (error) {
    setNotice($("login-message"), error.message || "读取扫码状态失败", true);
  }
}
setInterval(pollLogin, 3000);

$("remove-credential").addEventListener("click", async () => {
  try {
    await api("POST", "credential/remove", {});
    setNotice($("login-message"), "已退出 B 站登录。");
    await loadState();
  } catch (error) {
    setNotice($("login-message"), error.message || "退出失败", true);
  }
});

$("cookie-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await api("POST", "credential/manual", {
      sessdata: $("sessdata").value,
      bili_jct: $("bili-jct").value,
      dedeuserid: $("dedeuserid").value,
      buvid3: $("buvid3").value,
    });
    event.target.reset();
    setNotice($("cookie-message"), "Cookie 已加密保存。");
    await loadState();
  } catch (error) {
    setNotice($("cookie-message"), error.message || "加密保存失败", true);
  } finally {
    button.disabled = false;
  }
});

await loadState();
