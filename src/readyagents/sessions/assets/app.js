(() => {
  const log = document.getElementById("log");
  const form = document.getElementById("form");
  const input = document.getElementById("text");
  let sessionId = null;
  const token = window.READYAGENTS_CHAT_TOKEN || "";
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = "Bearer " + token;

  function line(role, text) {
    const item = document.createElement("li");
    item.textContent = role + ": " + text;
    log.appendChild(item);
  }

  async function ensure() {
    if (sessionId) return;
    const res = await fetch("/v1/sessions", { method: "POST", headers, body: "{}" });
    const data = await res.json();
    sessionId = data.session_id;
    if (data.say) line("assistant", data.say);
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value;
    input.value = "";
    await ensure();
    line("user", text);
    const res = await fetch("/v1/sessions/" + sessionId + "/turns", {
      method: "POST",
      headers,
      body: JSON.stringify({ text }),
    });
    const data = await res.json();
    if (data.say) line("assistant", data.say);
  });
})();
