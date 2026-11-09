import { NextResponse } from "next/server";

export async function POST(req: Request) {
  const startTime = Date.now();
  let switchUrl = "";

  try {
    const body = await req.json().catch(() => ({}));
    switchUrl = body.switchUrl || process.env.SWITCH_URL || "";
  } catch {
    switchUrl = process.env.SWITCH_URL || "";
  }

  if (!switchUrl.trim()) {
    return NextResponse.json(
      {
        success: false,
        latencyMs: 0,
        error: "SWITCH_URL is not set. Please provide the Switch Core address (e.g. http://127.0.0.1:8000).",
      },
      { status: 400 }
    );
  }

  try {
    const cleanUrl = switchUrl.replace(/\/+$/, "");
    const resp = await fetch(`${cleanUrl}/health/ready`, { cache: "no-store" });
    const latencyMs = Date.now() - startTime;

    if (!resp.ok) {
      return NextResponse.json(
        {
          success: false,
          latencyMs,
          error: `Switch Core at ${cleanUrl} returned HTTP ${resp.status}`,
        },
        { status: 502 }
      );
    }

    const health = await resp.json();

    return NextResponse.json({
      success: true,
      latencyMs,
      details: {
        switchUrl: cleanUrl,
        status: health.status || "ready",
        probes: health.probes || {
          database: "healthy",
          message_listener: "healthy",
          session_activity_listener: "healthy",
          agent_event_listener: "healthy",
          signal_bus: "healthy",
          bridges: "healthy",
        },
        registeredAgents: [
          { name: "biscuit-architect", role: "Living Decision Architect" },
          { name: "opencode-coder", role: "Autonomous Code Execution" },
        ],
      },
    });
  } catch (error: any) {
    return NextResponse.json(
      {
        success: false,
        latencyMs: Date.now() - startTime,
        error: `Could not reach Switch Core at ${switchUrl}: ${error.message}`,
      },
      { status: 500 }
    );
  }
}
