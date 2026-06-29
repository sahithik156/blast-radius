/*
RiskList.jsx

PURPOSE OF THIS FILE:
Renders the ranked list of files by risk score, this is the "answer card"
view from our earlier mockup, now driven by real data from the API instead
of hardcoded mock numbers. Each card shows the score, the underlying
breakdown stats (fan-in, bugfix commits, recency), and is clickable to sync
selection with the graph view below it.
*/

function riskLevelClass(score) {
  // WHY WE BUCKET INTO 3 LEVELS INSTEAD OF JUST SHOWING THE RAW NUMBER'S
  // COLOR DIRECTLY: a continuous color gradient (e.g. computing an exact
  // RGB value from the score) would be visually noisy and hard to scan
  // quickly, since two files scoring 81 and 84 would look like different
  // colors despite being practically the same risk level. Three clear
  // buckets (high/medium/low) let the user scan the list at a glance and
  // immediately spot the genuinely high-risk files.
  if (score >= 70) return 'risk-high'
  if (score >= 40) return 'risk-medium'
  return 'risk-low'
}

function RiskList({ files, summary, selectedFile, onSelectFile }) {
  return (
    <div className="risk-list">
      <div className="risk-list-summary">
        <span>{summary.total_files} files</span>
        <span>{summary.total_functions} functions</span>
        <span>{summary.total_dependency_edges} dependencies</span>
      </div>

      <div className="risk-list-items">
        {files.map((file) => {
          const b = file.breakdown
          const isSelected = file.filepath === selectedFile

          return (
            <div
              key={file.filepath}
              className={`risk-card ${riskLevelClass(file.risk_score)} ${isSelected ? 'selected' : ''}`}
              onClick={() => onSelectFile(file.filepath)}
            >
              <div className="risk-card-header">
                <code className="filepath">{file.filepath}</code>
                <span className="risk-score">{file.risk_score}</span>
              </div>

              <div className="risk-card-stats">
                <span>{b.fanin_count} files depend on this</span>
                <span>
                  {b.bugfix_count} bugfix-looking commits (of {b.commit_count} total)
                </span>
                <span>
                  {b.days_since_last_commit >= 0
                    ? `last changed ${b.days_since_last_commit} days ago`
                    : 'no history data'}
                </span>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default RiskList
