import Link from "next/link";

export default function NotFound() {
  return (
    <main className="not-found-page">
      <div className="not-found-card">
        <span className="not-found-code">404 / LOST IN THE THREAD</span>
        <h1>This page went<br /><em>off the record.</em></h1>
        <p>The page you&apos;re looking for isn&apos;t here. The project memory is still intact.</p>
        <Link className="button button--red" href="/">
          Back to Biscuit <span aria-hidden="true">↗</span>
        </Link>
        <span className="not-found-mark" aria-hidden="true">B.</span>
      </div>
    </main>
  );
}
