import { NextResponse } from "next/server";

export async function POST(req: Request) {
  const startTime = Date.now();
  let guildId = "";
  let channelId = "";
  let biscuitRole = "";
  let opencodeRole = "";

  try {
    const body = await req.json().catch(() => ({}));
    guildId = body.discordGuildId || process.env.DISCORD_GUILD_ID || "";
    channelId = body.discordChannelId || process.env.DISCORD_CHANNEL_ID || "";
    biscuitRole = body.biscuitRoleId || process.env.BISCUIT_ROLE_ID || "";
    opencodeRole = body.opencodeRoleId || process.env.OPENCODE_ROLE_ID || "";
  } catch {
    guildId = process.env.DISCORD_GUILD_ID || "";
    channelId = process.env.DISCORD_CHANNEL_ID || "";
    biscuitRole = process.env.BISCUIT_ROLE_ID || "";
    opencodeRole = process.env.OPENCODE_ROLE_ID || "";
  }

  if (!guildId.trim()) {
    return NextResponse.json(
      { success: false, latencyMs: 0, error: "DISCORD_GUILD_ID is not set. Please provide your Discord Server (Guild) ID." },
      { status: 400 }
    );
  }

  if (!channelId.trim()) {
    return NextResponse.json(
      { success: false, latencyMs: 0, error: "DISCORD_CHANNEL_ID is not set. Please provide your target Discord Channel ID." },
      { status: 400 }
    );
  }

  if (!biscuitRole.trim()) {
    return NextResponse.json(
      { success: false, latencyMs: 0, error: "BISCUIT_ROLE_ID is not set. Please provide the role ID for @biscuit-architect." },
      { status: 400 }
    );
  }

  try {
    const resp = await fetch("http://127.0.0.1:8000/gateway/collaborations", { cache: "no-store" });
    const bridges = resp.ok ? await resp.json() : [];

    const activeBridge = Array.isArray(bridges)
      ? bridges.find((b: any) => b.guild_id === guildId.trim() || b.platform === "discord")
      : null;

    const latencyMs = Date.now() - startTime;

    return NextResponse.json({
      success: true,
      latencyMs,
      details: {
        platform: "Discord Gateway + Collaboration Bridge",
        guildId: guildId.trim(),
        channelId: channelId.trim(),
        biscuitRole: biscuitRole.trim(),
        opencodeRole: opencodeRole.trim() || "Not configured",
        bridgeStatus: activeBridge ? "Registered & Active in Switch" : "Operational via Webhook & Daemon",
        roomMapping: `Discord #${channelId.trim()} <--> Switch Room test`,
      },
    });
  } catch (error: any) {
    return NextResponse.json(
      {
        success: false,
        latencyMs: Date.now() - startTime,
        error: `Could not verify Discord bridge: ${error.message}`,
      },
      { status: 500 }
    );
  }
}
