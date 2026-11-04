"use strict";

const connectButton = document.querySelector("#connect");
const disconnectButton = document.querySelector("#disconnect");
const statusNode = document.querySelector("#status");
const transcriptNode = document.querySelector("#transcript");
const remoteAudio = document.querySelector("#remote-audio");

let peer = null;
let events = null;
let localStream = null;
let conversationId = crypto.randomUUID();
let transcript = [];
const partial = { user: "", assistant: "" };
const activeDelegations = new Set();

function setStatus(text, state) {
  statusNode.textContent = text;
  statusNode.dataset.state = state;
}

function addTurn(role, text, className = "") {
  if (!text || !text.trim()) return;
  transcript.push({ role, text: text.trim() });
  transcript = transcript.slice(-40);
  const row = document.createElement("p");
  row.className = `turn ${className || role}`;
  const label = document.createElement("strong");
  label.textContent = role === "user" ? "You: " : role === "assistant" ? "GPT-Live: " : "System: ";
  row.append(label, document.createTextNode(text.trim()));
  transcriptNode.append(row);
  transcriptNode.scrollTop = transcriptNode.scrollHeight;
}

function transcriptPacket() {
  return transcript.slice(-16).map((turn) => `${turn.role.toUpperCase()}: ${turn.text}`).join("\n").slice(-15000);
}

function waitForIceComplete(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      if (pc.iceGatheringState === "complete") {
        pc.removeEventListener("icegatheringstatechange", done);
        resolve();
      }
    };
    pc.addEventListener("icegatheringstatechange", done);
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json().catch(() => ({ error: "invalid server response" }));
  if (!response.ok) throw new Error(typeof body.error === "string" ? body.error : "request failed");
  return body;
}

function sendCommentary(delegationId, content) {
  if (!events || events.readyState !== "open") return;
  events.send(JSON.stringify({
    type: "session.commentary.append",
    event_id: crypto.randomUUID(),
    delegation_id: delegationId,
    content,
  }));
}

async function pollTask(taskId, delegationId) {
  const deadline = Date.now() + 610000;
  while (Date.now() < deadline) {
    const task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    if (task.status === "succeeded") {
      sendCommentary(delegationId, task.result || "Axon completed the task without a textual result.");
      addTurn("system", "Axon returned a result.", "system");
      return;
    }
    if (["failed", "timed_out"].includes(task.status)) {
      sendCommentary(delegationId, `Axon could not complete the request: ${task.error || task.status}.`);
      addTurn("system", `Axon task ${task.status}.`, "system");
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  sendCommentary(delegationId, "Axon did not return before the voice bridge timeout.");
}

async function handleDelegation(event) {
  const id = event.delegation?.id || event.delegation_id || event.id;
  if (!id || activeDelegations.has(id)) return;
  activeDelegations.add(id);
  addTurn("system", "Delegating the latest bounded request to Axon…", "system");
  try {
    const task = await api("/api/delegate", {
      method: "POST",
      body: JSON.stringify({
        delegation_id: id,
        conversation_id: conversationId,
        transcript: transcriptPacket(),
        offset_ms: event.delegation?.offset_ms ?? event.offset_ms ?? null,
      }),
    });
    await pollTask(task.task_id, id);
  } catch (error) {
    const message = error instanceof Error ? error.message : "unknown delegation error";
    addTurn("system", `Delegation failed: ${message}`, "system");
    sendCommentary(id, `Axon delegation failed: ${message}`);
  } finally {
    activeDelegations.delete(id);
  }
}

function handleEvent(message) {
  let event;
  try { event = JSON.parse(message.data); } catch { return; }
  if (event.type === "session.started") setStatus("Connected · listening", "listening");
  if (event.type === "session.closed") disconnect("Session closed");
  if (event.type === "session.delegation.created") void handleDelegation(event);

  const delta = event.delta || event.text || "";
  if (event.type === "session.input_transcript.delta") partial.user += delta;
  if (event.type === "session.output_transcript.delta") partial.assistant += delta;
  if (event.type === "session.input_transcript.done") {
    addTurn("user", event.transcript || partial.user);
    partial.user = "";
  }
  if (event.type === "session.output_transcript.done") {
    addTurn("assistant", event.transcript || partial.assistant);
    partial.assistant = "";
  }
  if (event.type === "error") {
    const detail = event.error?.message || "GPT-Live session error";
    addTurn("system", detail, "system");
    setStatus("Error", "error");
  }
}

async function connect() {
  connectButton.disabled = true;
  setStatus("Connecting…", "connecting");
  try {
    conversationId = crypto.randomUUID();
    localStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    peer = new RTCPeerConnection();
    peer.ontrack = (event) => { remoteAudio.srcObject = event.streams[0]; };
    peer.onconnectionstatechange = () => {
      if (peer && ["failed", "disconnected", "closed"].includes(peer.connectionState)) {
        disconnect(`Connection ${peer.connectionState}`);
      }
    };
    localStream.getTracks().forEach((track) => peer.addTrack(track, localStream));
    events = peer.createDataChannel("oai-events");
    events.onmessage = handleEvent;
    events.onopen = () => setStatus("Connected · listening", "connected");
    events.onclose = () => disconnect("Disconnected");

    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);
    await waitForIceComplete(peer);
    const session = await api("/api/live/session", {
      method: "POST",
      body: JSON.stringify({ sdp: peer.localDescription.sdp }),
    });
    const answerSdp = session.transport?.sdp;
    if (!answerSdp) throw new Error("OpenAI did not return WebRTC SDP");
    await peer.setRemoteDescription({ type: "answer", sdp: answerSdp });
    disconnectButton.disabled = false;
  } catch (error) {
    const message = error instanceof Error ? error.message : "connection failed";
    addTurn("system", message, "system");
    disconnect("Connection failed", true);
  }
}

function disconnect(reason = "Disconnected", keepButton = false) {
  if (events) events.close();
  if (peer) peer.close();
  if (localStream) localStream.getTracks().forEach((track) => track.stop());
  events = null;
  peer = null;
  localStream = null;
  remoteAudio.srcObject = null;
  disconnectButton.disabled = true;
  connectButton.disabled = false;
  setStatus(reason, reason.includes("failed") ? "error" : "disconnected");
  if (!keepButton) activeDelegations.clear();
}

connectButton.addEventListener("click", () => void connect());
disconnectButton.addEventListener("click", () => disconnect());
