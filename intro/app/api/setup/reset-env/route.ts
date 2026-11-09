import { NextResponse } from "next/server";
import fs from "node:fs";

const BISCUIT_ENV_PATH = "/Users/adarshchauhan/Downloads/biscuit/.env";

export async function POST() {
  try {
    if (fs.existsSync(BISCUIT_ENV_PATH)) {
      fs.unlinkSync(BISCUIT_ENV_PATH);
    }
    return NextResponse.json({
      success: true,
      message: "Credentials file cleared. System is now completely unconfigured.",
    });
  } catch (e: any) {
    return NextResponse.json(
      { success: false, error: e.message || "Failed to reset .env file" },
      { status: 500 }
    );
  }
}
