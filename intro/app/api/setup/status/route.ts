import { NextResponse } from "next/server";
import fs from "node:fs";

const BISCUIT_ENV_PATH = "/Users/adarshchauhan/Downloads/biscuit/.env";

function parseEnvFile(filePath: string): Record<string, string> {
  const result: Record<string, string> = {};
  if (!fs.existsSync(filePath)) return result;
  try {
    const content = fs.readFileSync(filePath, "utf-8");
    for (const line of content.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const eqIdx = trimmed.indexOf("=");
      if (eqIdx > 0) {
        const key = trimmed.slice(0, eqIdx).trim();
        let val = trimmed.slice(eqIdx + 1).trim();
        if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
          val = val.slice(1, -1);
        }
        result[key] = val;
      }
    }
  } catch (e) {
    console.error("Error reading env file:", filePath, e);
  }
  return result;
}

export async function GET() {
  const envExists = fs.existsSync(BISCUIT_ENV_PATH);
  const biscuitEnv = parseEnvFile(BISCUIT_ENV_PATH);

  const databaseUrl = biscuitEnv.DATABASE_URL || "";
  const switchUrl = biscuitEnv.SWITCH_URL || "";
  const switchWsUrl = biscuitEnv.SWITCH_WS_URL || "";
  const llmEndpoint = biscuitEnv.LOCAL_MODEL_ENDPOINT || "";
  const llmApiKey = biscuitEnv.LOCAL_MODEL_API_KEY || "";
  const llmModel = biscuitEnv.LOCAL_MODEL_NAME || "";
  const githubAppId = biscuitEnv.GITHUB_APP_ID || "";
  const githubInstallationId = biscuitEnv.GITHUB_INSTALLATION_ID || "";
  const githubPemPath = biscuitEnv.GITHUB_APP_PRIVATE_KEY_PATH || "";
  const targetRepo = biscuitEnv.TARGET_REPO || "";
  const discordBotToken = biscuitEnv.DISCORD_BOT_TOKEN || "";
  const discordGuildId = biscuitEnv.DISCORD_GUILD_ID || "";
  const discordChannelId = biscuitEnv.DISCORD_CHANNEL_ID || "";
  const biscuitRoleId = biscuitEnv.BISCUIT_ROLE_ID || "";
  const opencodeRoleId = biscuitEnv.OPENCODE_ROLE_ID || "";

  return NextResponse.json({
    envExists,
    filePath: BISCUIT_ENV_PATH,
    formValues: {
      databaseUrl,
      switchUrl,
      switchWsUrl,
      llmEndpoint,
      llmApiKey,
      llmModel,
      githubAppId,
      githubInstallationId,
      githubPemPath,
      targetRepo,
      discordBotToken,
      discordGuildId,
      discordChannelId,
      biscuitRoleId,
      opencodeRoleId,
    },
    components: {
      database: {
        configured: Boolean(databaseUrl),
        value: databaseUrl,
      },
      switch: {
        configured: Boolean(switchUrl),
        value: switchUrl,
      },
      llm: {
        configured: Boolean(llmEndpoint && llmApiKey),
        value: llmEndpoint,
      },
      github: {
        configured: Boolean(githubAppId && githubInstallationId && githubPemPath && targetRepo),
        value: targetRepo,
      },
      discord: {
        configured: Boolean(discordGuildId && discordChannelId && biscuitRoleId),
        value: discordChannelId,
      },
    },
    templateSnippet: `# ── Biscuit & Switch Environment Configuration ──
DATABASE_URL=postgresql://switch_app:a202deadabf3d139eb242465ce3cfa68c51066618c621b32@localhost:5433/switch
SWITCH_URL=http://127.0.0.1:8000
SWITCH_WS_URL=ws://127.0.0.1:8000
LOCAL_MODEL_ENDPOINT=http://localhost:5580/v1
LOCAL_MODEL_API_KEY=sk-pkpi5wen5ifxugoer2t1sl2k8l2gprsj3eyk71lxl98vp6pz
GITHUB_APP_ID=5631771
GITHUB_INSTALLATION_ID=179018445
GITHUB_APP_PRIVATE_KEY_PATH=/Users/adarshchauhan/Downloads/biscuitt-hemlooo.2026-11-05.private-key.pem
TARGET_REPO=MRIARC-08/test
DISCORD_GUILD_ID=1568866378147041342
DISCORD_CHANNEL_ID=1568866379095081124
BISCUIT_ROLE_ID=1569289793622511677
OPENCODE_ROLE_ID=1569289760260890644
`,
  });
}
