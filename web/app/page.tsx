import { Mark } from "@/components/Mark";

const REPO = "https://github.com/Afeezee/Freehold";
const RELEASES = `${REPO}/releases/latest`;

export default function Home() {
  return (
    <>
      <a className="skip" href="#main">Skip to content</a>

      <header className="nav">
        <div className="wrap nav-inner">
          <a className="brand" href="/">
            <Mark /> Freehold
          </a>
          <nav className="nav-links" aria-label="Main">
            <a href="#how">How it works</a>
            <a href="#download">Download</a>
            <a className="hide-sm" href={`${REPO}/blob/main/README.md`}>Docs</a>
            <a href={REPO}>Source</a>
          </nav>
        </div>
      </header>

      <main id="main">
        <div className="wrap hero">
          <h1>Backups only you can read.</h1>
          <p className="lead">
            Freehold encrypts your files on your own computer, then stores the
            scrambled pieces wherever you choose — a spare drive, a GitHub
            account, Cloudflare R2. The provider cannot read them, and you can
            move to a different one whenever you like.
          </p>
          <div className="hero-actions">
            <a className="btn primary btn-col" href={RELEASES}>
              Download for Windows
              <small>Free · no account needed</small>
            </a>
            <a className="btn btn-col" href="#download">
              Android and other platforms
              <small>APK, or run from source</small>
            </a>
          </div>
          <p className="hero-note">
            There is no sign-up, because there is nothing to sign up to.
            Everything runs on your machine.
          </p>
        </div>

        {/* ── how ─────────────────────────────────────────────── */}
        <section id="how">
          <div className="wrap">
            <h2>How it works</h2>
            <p className="sub">
              Four steps, all of which happen before anything leaves your
              computer.
            </p>
            <div className="steps">
              <div className="step">
                <h3>1. Split</h3>
                <p>
                  Your folder is divided into blocks of about a megabyte,
                  cut at boundaries decided by the content itself.
                </p>
              </div>
              <div className="step">
                <h3>2. Skip what is known</h3>
                <p>
                  Each block is fingerprinted. Blocks that have been stored
                  before are not sent again, so editing a document uploads
                  about 100 KB, not the whole file.
                </p>
              </div>
              <div className="step">
                <h3>3. Encrypt</h3>
                <p>
                  Every block is sealed with AES-256-GCM under a key that is
                  generated on your machine and never leaves it.
                </p>
              </div>
              <div className="step">
                <h3>4. Store anywhere</h3>
                <p>
                  The sealed blocks go to storage you control. Add a second
                  place and copy everything across in one command.
                </p>
              </div>
            </div>

            <div className="callout">
              <h3>The part you must not skip</h3>
              <p>
                Because only your key can decrypt your backups, losing that
                key means losing the backups — permanently, with no way for
                anyone to help. Freehold tells you where the key file is the
                first time it runs. Copy it somewhere separate before you
                rely on any of this.
              </p>
            </div>
          </div>
        </section>

        {/* ── why ─────────────────────────────────────────────── */}
        <section id="why">
          <div className="wrap">
            <h2>What makes it different</h2>
            <p className="sub">
              Most backup services make one company load-bearing. If the
              account is suspended or the price changes, getting your files
              out is their decision as much as yours.
            </p>
            <div className="grid">
              <div className="cell">
                <h3>The provider cannot read your files</h3>
                <p>
                  Encryption happens before upload. Storage sees scrambled
                  blocks under meaningless names, and cannot tell whether a
                  particular file is among them.
                </p>
              </div>
              <div className="cell">
                <h3>Switching is a copy, not a migration</h3>
                <p>
                  Add new storage, run one command to fill it, confirm it can
                  serve your data, then drop the old one. Nothing is
                  re-uploaded from your machine.
                </p>
              </div>
              <div className="cell">
                <h3>It works without Freehold</h3>
                <p>
                  Every format is documented, and a single standalone script
                  can rebuild your files using only standard tools. The app is
                  the convenient path, not the only one.
                </p>
              </div>
              <div className="cell">
                <h3>A dead computer is recoverable</h3>
                <p>
                  Your key and your storage credentials are enough. The index
                  of what is backed up is itself stored, encrypted, so a
                  replacement machine can find and rebuild it.
                </p>
              </div>
              <div className="cell">
                <h3>Nothing is hidden when it goes wrong</h3>
                <p>
                  A backup that stores nothing is reported as failed, not
                  silently filed as done. Files that could not be stored are
                  counted and named.
                </p>
              </div>
              <div className="cell">
                <h3>Free storage is usually enough</h3>
                <p>
                  GitHub, Cloudflare R2 and Backblaze B2 all have free tiers
                  around 10 GB, and a drive you already own costs nothing.
                </p>
              </div>
            </div>
          </div>
        </section>

        {/* ── download ────────────────────────────────────────── */}
        <section id="download">
          <div className="wrap">
            <h2>Get Freehold</h2>
            <p className="sub">
              Every download is built in public from the source in this
              repository, and contains no credentials of any kind.
            </p>

            <div className="downloads">
              <div className="dl">
                <h3>Windows</h3>
                <p>
                  A single file. Run it and it sets itself up, then opens in
                  your browser. No Python, no installer.
                </p>
                <span className="meta">Freehold.exe · about 14 MB</span>
                <a className="btn primary" href={RELEASES}>Download</a>
              </div>

              <div className="dl">
                <h3>Android</h3>
                <p>
                  Backs up your photos and documents using the same engine.
                  Sideloaded, so Android will ask you to allow it.
                </p>
                <span className="meta">app-debug.apk</span>
                <a className="btn" href={RELEASES}>Download</a>
              </div>

              <div className="dl">
                <h3>macOS, Linux, or from source</h3>
                <p>
                  Requires Python 3.9 or later. The command line and the
                  browser interface are the same program.
                </p>
                <span className="meta">pip install -e .</span>
                <a className="btn" href={`${REPO}#installing`}>Instructions</a>
              </div>
            </div>

            <div className="callout">
              <h3>Why there is no “sign in”</h3>
              <p>
                An account would mean a server holding either your key or your
                files, and that is the arrangement Freehold exists to avoid.
                Your identity here is your key file: whoever holds it can read
                the backups, and nobody else can — including us, because there
                is no us to ask.
              </p>
            </div>
          </div>
        </section>

        {/* ── start ───────────────────────────────────────────── */}
        <section id="start">
          <div className="wrap">
            <h2>Five minutes to your first backup</h2>
            <p className="sub">
              Using a spare drive, with no accounts or tokens involved.
            </p>
            <pre><code>{`freehold init
freehold backend add usb local path=E:/backups
freehold backup ~/Documents
freehold restore ~/Recovered`}</code></pre>
            <p className="sub" style={{ marginTop: "var(--s5)" }}>
              Or run <code className="inline">Freehold.exe</code> and do the
              same thing by pointing and clicking.
            </p>
          </div>
        </section>

        {/* ── honesty ─────────────────────────────────────────── */}
        <section id="status">
          <div className="wrap">
            <h2>What is finished, and what is not</h2>
            <p className="sub">
              Backup software that overstates itself is worse than none, so
              here is the current state without the marketing.
            </p>
            <table className="cmp">
              <thead>
                <tr><th scope="col">Part</th><th scope="col">State</th></tr>
              </thead>
              <tbody>
                <tr>
                  <td>Core engine, Windows app, command line</td>
                  <td>Working. 165 automated tests.</td>
                </tr>
                <tr>
                  <td>GitHub storage</td>
                  <td>
                    Verified end to end against the live API, including
                    recovering a destroyed machine from the key alone.
                  </td>
                </tr>
                <tr>
                  <td>Cloudflare R2, Backblaze B2</td>
                  <td>
                    Implemented and tested against a local server; not yet
                    run against a real provider account.
                  </td>
                </tr>
                <tr>
                  <td>Android app</td>
                  <td>Written, not yet built or run on a handset.</td>
                </tr>
                <tr>
                  <td>iOS app</td>
                  <td>Not started. Needs the engine ported first.</td>
                </tr>
                <tr>
                  <td>Very large backups</td>
                  <td>
                    Untested beyond a few gigabytes. Splitting runs at about
                    12 MB/s, so a first backup of a large library is slow.
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      </main>

      <footer>
        <div className="wrap foot-grid">
          <div>
            <a className="brand" href="/" style={{ marginBottom: "8px" }}>
              <Mark size={20} /> Freehold
            </a>
            <p style={{ margin: 0, maxWidth: "34ch" }}>
              Open source under the AGPL-3.0. Free to use, study, modify
              and share; improvements to public versions come back.
            </p>
          </div>
          <div>
            <h4>Product</h4>
            <a href="#how">How it works</a>
            <a href="#download">Download</a>
            <a href="#status">What is finished</a>
          </div>
          <div>
            <h4>Documentation</h4>
            <a href={`${REPO}/blob/main/README.md`}>Guide</a>
            <a href={`${REPO}/blob/main/RECOVERY.md`}>Recovering without Freehold</a>
            <a href={`${REPO}/blob/main/docs/DESIGN.md`}>Design</a>
          </div>
          <div>
            <h4>Project</h4>
            <a href={REPO}>Source code</a>
            <a href={`${REPO}/issues`}>Report a problem</a>
            <a href={`${REPO}/blob/main/LICENSE`}>Licence</a>
          </div>
        </div>
      </footer>
    </>
  );
}
