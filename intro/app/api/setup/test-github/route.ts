import { NextResponse } from "next/server";
import fs from "node:fs";
import { exec } from "node:child_process";
import { promisify } from "node:util";

const execAsync = promisify(exec);

export async function POST(req: Request) {
  const startTime = Date.now();
  let appId = "";
  let installationId = "";
  let pemPath = "";
  let targetRepo = "";

  try {
    const body = await req.json().catch(() => ({}));
    appId = body.githubAppId || process.env.GITHUB_APP_ID || "";
    installationId = body.githubInstallationId || process.env.GITHUB_INSTALLATION_ID || "";
    pemPath = body.githubPemPath || process.env.GITHUB_APP_PRIVATE_KEY_PATH || "";
    targetRepo = body.targetRepo || process.env.TARGET_REPO || "";
  } catch {
    appId = process.env.GITHUB_APP_ID || "";
    installationId = process.env.GITHUB_INSTALLATION_ID || "";
    pemPath = process.env.GITHUB_APP_PRIVATE_KEY_PATH || "";
    targetRepo = process.env.TARGET_REPO || "";
  }

  if (!appId.trim()) {
    return NextResponse.json(
      { success: false, latencyMs: 0, error: "GITHUB_APP_ID is not set. Please provide your GitHub App ID." },
      { status: 400 }
    );
  }

  if (!installationId.trim()) {
    return NextResponse.json(
      { success: false, latencyMs: 0, error: "GITHUB_INSTALLATION_ID is not set. Please provide your App Installation ID." },
      { status: 400 }
    );
  }

  if (!pemPath.trim()) {
    return NextResponse.json(
      { success: false, latencyMs: 0, error: "GITHUB_APP_PRIVATE_KEY_PATH is not set. Please provide the path to your PEM file." },
      { status: 400 }
    );
  }

  if (!fs.existsSync(pemPath.trim())) {
    return NextResponse.json(
      {
        success: false,
        latencyMs: Date.now() - startTime,
        error: `PEM private key file not found on disk at: ${pemPath}`,
      },
      { status: 404 }
    );
  }

  if (!targetRepo.trim()) {
    return NextResponse.json(
      { success: false, latencyMs: 0, error: "TARGET_REPO is not set. Please provide target repo (e.g. owner/repo)." },
      { status: 400 }
    );
  }

  try {
    const { stdout } = await execAsync(`gh repo view ${targetRepo.trim()} --json name,owner,defaultBranchRef,isPrivate`);
    const repoData = JSON.parse(stdout);
    const latencyMs = Date.now() - startTime;

    return NextResponse.json({
      success: true,
      latencyMs,
      details: {
        appId: appId.trim(),
        installationId: installationId.trim(),
        pemStatus: "Verified on disk",
        targetRepository: targetRepo.trim(),
        repoOwner: repoData.owner?.login || targetRepo.split("/")[0],
        defaultBranch: repoData.defaultBranchRef?.name || "main",
        isPrivate: repoData.isPrivate ?? false,
      },
    });
  } catch (error: any) {
    return NextResponse.json(
      {
        success: false,
        latencyMs: Date.now() - startTime,
        error: `Failed to query repository ${targetRepo}: ${error.message}`,
      },
      { status: 500 }
    );
  }
}
