import { NextResponse } from "next/server";

export async function POST(req: Request) {
  const startTime = Date.now();
  let endpoint = "";
  let apiKey = "";
  let model = "simple-task";

  try {
    const body = await req.json().catch(() => ({}));
    endpoint = body.llmEndpoint || process.env.LOCAL_MODEL_ENDPOINT || "";
    apiKey = body.llmApiKey || process.env.LOCAL_MODEL_API_KEY || "";
    model = body.llmModel || process.env.LOCAL_MODEL_NAME || "simple-task";
  } catch {
    endpoint = process.env.LOCAL_MODEL_ENDPOINT || "";
    apiKey = process.env.LOCAL_MODEL_API_KEY || "";
  }

  if (!endpoint.trim()) {
    return NextResponse.json(
      {
        success: false,
        latencyMs: 0,
        error: "LOCAL_MODEL_ENDPOINT is not set. Please provide the inference URL (e.g. http://localhost:5580/v1).",
      },
      { status: 400 }
    );
  }

  if (!apiKey.trim()) {
    return NextResponse.json(
      {
        success: false,
        latencyMs: 0,
        error: "LOCAL_MODEL_API_KEY is not set. Please provide a Bearer API key or token.",
      },
      { status: 400 }
    );
  }

  try {
    const cleanUrl = endpoint.replace(/\/+$/, "");
    const resp = await fetch(`${cleanUrl}/chat/completions`, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${apiKey.trim()}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model,
        messages: [
          { role: "system", content: "You are a health probe. Reply with PONG." },
          { role: "user", content: "PING" },
        ],
        max_tokens: 10,
      }),
      cache: "no-store",
    });

    const latencyMs = Date.now() - startTime;

    if (!resp.ok) {
      const errText = await resp.text();
      return NextResponse.json(
        {
          success: false,
          latencyMs,
          error: `Inference endpoint ${cleanUrl} returned HTTP ${resp.status}: ${errText}`,
        },
        { status: 502 }
      );
    }

    const data = await resp.json();
    const reply = data.choices?.[0]?.message?.content?.trim() || "PONG";

    return NextResponse.json({
      success: true,
      latencyMs,
      details: {
        endpoint: cleanUrl,
        model,
        probeReply: reply,
        status: "Active & Generating",
      },
    });
  } catch (error: any) {
    return NextResponse.json(
      {
        success: false,
        latencyMs: Date.now() - startTime,
        error: `Could not reach LLM inference endpoint at ${endpoint}: ${error.message}`,
      },
      { status: 500 }
    );
  }
}
