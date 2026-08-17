"use strict";

const csrfToken = document.querySelector('meta[name="samye-token"]').content;
const proposalsNode = document.querySelector("#proposals");
const messageNode = document.querySelector("#message");

function tokens(text) {
  return text.match(/\s+|[^\s]+/gu) || [];
}

function wordDiff(before, after) {
  const left = tokens(before);
  const right = tokens(after);
  const table = Array.from({ length: left.length + 1 }, () =>
    new Uint32Array(right.length + 1),
  );
  for (let i = left.length - 1; i >= 0; i -= 1) {
    for (let j = right.length - 1; j >= 0; j -= 1) {
      table[i][j] = left[i] === right[j]
        ? table[i + 1][j + 1] + 1
        : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const leftParts = [];
  const rightParts = [];
  let i = 0;
  let j = 0;
  while (i < left.length || j < right.length) {
    if (i < left.length && j < right.length && left[i] === right[j]) {
      leftParts.push([left[i], false]);
      rightParts.push([right[j], false]);
      i += 1;
      j += 1;
    } else if (j < right.length && (i === left.length || table[i][j + 1] >= table[i + 1][j])) {
      rightParts.push([right[j], true]);
      j += 1;
    } else {
      leftParts.push([left[i], true]);
      i += 1;
    }
  }
  return { leftParts, rightParts };
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function renderParts(parent, parts, changedTag) {
  for (const [text, changed] of parts) {
    parent.append(changed ? element(changedTag, text) : document.createTextNode(text));
  }
}

function diffPane(label, parts, changedTag) {
  const pane = element("div", undefined, "pane");
  pane.append(element("h3", label));
  const pre = element("pre");
  renderParts(pre, parts, changedTag);
  pane.append(pre);
  return pane;
}

function statusNotice(status) {
  if (status === "stale") return "The source text changed. Re-trigger from the document.";
  if (status === "indeterminate") return "Verify the document manually before re-triggering.";
  if (status === "applying") return "The edit is currently being applied.";
  return null;
}

async function transition(proposal, action, buttons) {
  for (const button of buttons) button.disabled = true;
  try {
    const response = await fetch(
      `/api/proposals/${encodeURIComponent(proposal.file_id)}/${encodeURIComponent(proposal.id)}/${action}`,
      { method: "POST", headers: { "X-Samye-Token": csrfToken } },
    );
    if (!response.ok) throw new Error(`request failed (${response.status})`);
    const result = await response.json();
    proposal.status = result.status;
    render(proposals);
  } catch (error) {
    messageNode.textContent = error.message;
    for (const button of buttons) button.disabled = false;
  }
}

function proposalCard(proposal) {
  const card = element("article", undefined, "proposal");
  card.append(element("h2", proposal.document_title));
  const meta = element("div", undefined, "meta");
  meta.append(element("span", proposal.status, "status"));
  meta.append(element("span", `${proposal.provider}/${proposal.model}`));
  meta.append(element("span", proposal.created));
  card.append(meta);

  const compared = wordDiff(proposal.target_text, proposal.replacement);
  const diff = element("div", undefined, "diff");
  diff.append(diffPane("Current", compared.leftParts, "del"));
  diff.append(diffPane("Proposed", compared.rightParts, "ins"));
  card.append(diff);

  const notice = statusNotice(proposal.status);
  if (notice) card.append(element("p", notice, "notice"));
  if (proposal.status === "pending") {
    const actions = element("div", undefined, "actions");
    const accept = element("button", "Accept");
    const reject = element("button", "Reject");
    const buttons = [accept, reject];
    accept.addEventListener("click", () => transition(proposal, "accept", buttons));
    reject.addEventListener("click", () => transition(proposal, "reject", buttons));
    actions.append(accept, reject);
    card.append(actions);
  }
  return card;
}

let proposals = [];

function render(items) {
  proposalsNode.replaceChildren(...items.map(proposalCard));
  messageNode.textContent = items.length === 0
    ? "No proposals"
    : `${items.length} proposal${items.length === 1 ? "" : "s"}`;
}

async function load() {
  try {
    const response = await fetch("/api/proposals");
    if (!response.ok) throw new Error(`request failed (${response.status})`);
    proposals = await response.json();
    render(proposals);
  } catch (error) {
    messageNode.textContent = error.message;
  }
}

load();
