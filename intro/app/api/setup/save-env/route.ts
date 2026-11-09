import { NextResponse } from "next/server";
import fs from "node:fs";

const BISCUIT_ENV_PATH = "/Users/adarshchauhan/Downloads/biscuit/.env";

export async function POST(req: Request) {
  try {
    const body = await req.json();
    const envLines: string[] = [
      "# ── Biscuit & Switch Environment Configuration ──",
      `# Generated from Setup Hub at ${new Date().toISOString()}`,
      "",
    ];

    const mapping: Record<string, string> = {
      databaseUrl: "DATABASE_URL",
      switchUrl: "SWITCH_URL",
      switchWsUrl: "SWITCH_WS_URL",
      llmEndpoint: "LOCAL_MODEL_ENDPOINT",
      llmApiKey: "LOCAL_MODEL_API_KEY",
      llmModel: "LOCAL_MODEL_NAME",
      githubAppId: "GITHUB_APP_ID",
      githubInstallationId: "GITHUB_INSTALLATION_ID",
      githubPemPath: "GITHUB_APP_PRIVATE_KEY_PATH",
      targetRepo: "TARGET_REPO",
      discordBotToken: "DISCORD_BOT_TOKEN",
      discordGuildId: "DISCORD_GUILD_ID",
      discordChannelId: "DISCORD_CHANNEL_ID",
      biscuitRoleId: "BISCUIT_ROLE_ID",
      opencodeRoleId: "OPENCODE_ROLE_ID",
    };

    for (const [key, envVar] of Object.entries(mapping)) {
      const val = body[key] !== undefined ? String(body[key]).trim() : "";
      if (val) {
        envLines.push(`${envVar}=${val}`);
      }
    }

    envLines.push("");
    fs.writeFileSync(BISCUIT_ENV_PATH, envLines.join("\n"), "utf-8");

    return NextResponse.json({
      success: true,
      message: `Saved configuration to ${BISCUIT_ENV_PATH}`,
      filePath: BISCUIT_ENV_PATH,
    });
  } catch (e: any) {
    return NextResponse.json(
      { success: false, error: e.message || "Failed to write .env file" },
      { status: 500 }
    );
  }
}
