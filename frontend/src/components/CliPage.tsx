import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import type { CliStatus } from "../api/types";
import { cliWsUrl, fetchCliStatus } from "../api/cli";

/**
 * CLI — a real interactive terminal wired to the CockroachDB Cloud CLI. The
 * backend spawns a login shell in a PTY (`ccloud` on PATH) and bridges it to
 * this xterm.js terminal over a WebSocket (`WS /cli/ws`). So `ccloud cluster
 * list`, `ccloud cluster sql …`, etc. run for real against the authed Cloud
 * account — felix exercising the ccloud CLI as a first-class, agent-ready
 * CockroachDB offering.
 *
 * The status banner (`GET /cli/status`) shows whether ccloud is installed and
 * which account it's authed as, so the operator sees the connection at a glance.
 */
export function CliPage() {
  const holderRef = useRef<HTMLDivElement | null>(null);
  const [status, setStatus] = useState<CliStatus | null>(null);
  const [connState, setConnState] = useState<"connecting" | "open" | "closed" | "mock">(
    "connecting",
  );

  useEffect(() => {
    fetchCliStatus()
      .then(setStatus)
      .catch(() => setStatus(null));
  }, []);

  useEffect(() => {
    const url = cliWsUrl();
    if (!url) {
      setConnState("mock");
      return;
    }
    const holder = holderRef.current;
    if (!holder) return;

    const term = new Terminal({
      convertEol: false,
      cursorBlink: true,
      fontFamily:
        'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace',
      fontSize: 13,
      theme: {
        background: "#0b0f14",
        foreground: "#d5dee8",
        cursor: "#f0883e",
        selectionBackground: "#2a313c",
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(holder);
    fit.fit();

    const dec = new TextDecoder();
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";

    const sendResize = () => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
      }
    };

    ws.onopen = () => {
      setConnState("open");
      sendResize();
      term.focus();
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") term.write(ev.data);
      else term.write(dec.decode(new Uint8Array(ev.data as ArrayBuffer)));
    };
    ws.onclose = () => {
      setConnState("closed");
      term.write("\r\n\x1b[38;5;208m[felix: terminal session closed]\x1b[0m\r\n");
    };
    ws.onerror = () => setConnState("closed");

    // keystrokes → pty
    const dataSub = term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "input", data }));
      }
    });

    // keep the pty window size in sync with the panel
    const onResize = () => {
      try {
        fit.fit();
      } catch {
        /* holder detached */
      }
      sendResize();
    };
    const ro = new ResizeObserver(onResize);
    ro.observe(holder);
    window.addEventListener("resize", onResize);

    return () => {
      window.removeEventListener("resize", onResize);
      ro.disconnect();
      dataSub.dispose();
      try {
        ws.close();
      } catch {
        /* already closing */
      }
      term.dispose();
    };
  }, []);

  return (
    <div className="cli">
      <div className="cli__head">
        <h2>
          CLI
          <span className="cli__via" title="A real terminal wired to the CockroachDB Cloud CLI">
            ccloud terminal
          </span>
        </h2>
        <p className="cli__sub">
          A live shell on the felix backend with <code>ccloud</code> on PATH — the CockroachDB
          Cloud CLI, agent-ready. Try <code>ccloud cluster list</code> or{" "}
          <code>ccloud cluster sql felix-db</code>.
        </p>
      </div>

      <CliBanner status={status} connState={connState} />

      {connState === "mock" ? (
        <div className="cli__mock">
          <strong>No backend connected.</strong>
          <p>
            The terminal needs a real shell on the API host. Start the backend (<code>
              python -m src serve
            </code>) and point <code>VITE_API_URL</code> at it, then reopen this tab.
          </p>
        </div>
      ) : (
        <div className="cli__termwrap">
          <div className="cli__term" ref={holderRef} />
        </div>
      )}
    </div>
  );
}

function CliBanner({
  status,
  connState,
}: {
  status: CliStatus | null;
  connState: "connecting" | "open" | "closed" | "mock";
}) {
  if (connState === "mock") return null;

  const dot =
    connState === "open" ? "cli__dot--ok" : connState === "closed" ? "cli__dot--err" : "cli__dot--wait";
  const label =
    connState === "open" ? "connected" : connState === "closed" ? "disconnected" : "connecting…";

  return (
    <div className="cli__banner">
      <span className="cli__conn">
        <span className={`cli__dot ${dot}`} />
        {label}
      </span>
      {status && (
        <>
          {status.ccloud_installed ? (
            <span className="cli__fact">
              <code>ccloud</code> {status.account ? `· ${status.account}` : "· not authed"}
            </span>
          ) : (
            <span className="cli__fact cli__fact--warn">
              ccloud not installed — <code>brew install cockroachdb/tap/ccloud</code> then{" "}
              <code>ccloud auth login</code>
            </span>
          )}
          {status.cluster_id && (
            <span className="cli__fact cli__fact--dim">cluster {status.cluster_id}</span>
          )}
        </>
      )}
    </div>
  );
}
