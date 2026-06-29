/*
App.jsx

PURPOSE OF THIS FILE:
This is the root, top-level component of the whole frontend. It owns the
ONE piece of state everything else depends on: the analysis result from
our API (the list of files with risk scores, and the dependency edges).
It renders three things, stacked vertically: a search bar (to type a
GitHub URL and trigger analysis), a risk-ranked file list (the "answer
card" view from our earlier mockup), and the interactive dependency graph.

WHY ALL THE STATE LIVES HERE, NOT IN CHILD COMPONENTS:
RepoInput needs to trigger a fetch. RiskList and DependencyGraph both need
the SAME analysis result to render their two different views of it. If each
child component fetched its own data independently, we'd either duplicate
the API call (wasteful, and they could get out of sync) or need some other
way to share data between siblings. Lifting the state up to the shared
parent (this file) and passing it down as props is the standard React
pattern for "multiple components need the same data."
*/

import { useState } from 'react'
import RepoInput from './components/RepoInput'
import RiskList from './components/RiskList'
import DependencyGraph from './components/DependencyGraph'
import './App.css'

function App() {
  // analysisResult holds the full JSON response from POST /analyze, once
  // we have one. null means "no analysis has been run yet."
  const [analysisResult, setAnalysisResult] = useState(null)

  // isLoading tracks whether a request is currently in flight, so we can
  // show a loading state instead of a blank screen during the (sometimes
  // lengthy) analysis pipeline.
  const [isLoading, setIsLoading] = useState(false)

  // errorMessage holds a user-facing error string if the last analysis
  // attempt failed, null means no error to show.
  const [errorMessage, setErrorMessage] = useState(null)

  // selectedFile tracks which file the user has clicked on (in either the
  // risk list OR the graph), so both views can highlight the SAME file in
  // sync with each other. This is exactly the "blast radius" interaction
  // from our mockup: clicking a node highlights it everywhere.
  const [selectedFile, setSelectedFile] = useState(null)

  async function handleAnalyze(githubUrl) {
    setIsLoading(true)
    setErrorMessage(null)
    setSelectedFile(null)

    try {
      const response = await fetch('http://localhost:8000/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ github_url: githubUrl }),
      })

      const data = await response.json()

      if (!response.ok) {
        // WHY WE READ data.detail HERE: our FastAPI backend returns
        // errors in the shape {"detail": "some message"} (this is
        // FastAPI's standard convention for HTTPException). Reading it
        // out here lets us show the SAME friendly error message we
        // built on the backend (e.g. "Could not access that
        // repository...") directly to the user, instead of a generic
        // "request failed" message.
        throw new Error(data.detail || 'Analysis failed')
      }

      setAnalysisResult(data)
    } catch (err) {
      // WHY WE CATCH BOTH NETWORK ERRORS AND THE explicit throw ABOVE:
      // fetch() itself throws if the server is completely unreachable
      // (e.g. the backend isn't running), separately from the case where
      // the server responds but with an error status. Catching both here
      // means the user sees SOME helpful message either way, rather than
      // an uncaught exception crashing the app.
      setErrorMessage(err.message)
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>Blast Radius</h1>
        <p className="subtitle">
          Paste a GitHub repo URL to see what breaks if you change each file.
        </p>
      </header>

      <RepoInput onAnalyze={handleAnalyze} isLoading={isLoading} />

      {errorMessage && <div className="error-banner">{errorMessage}</div>}

      {analysisResult && (
        <div className="results-layout">
          <RiskList
            files={analysisResult.files}
            summary={analysisResult.summary}
            selectedFile={selectedFile}
            onSelectFile={setSelectedFile}
          />
          <DependencyGraph
            files={analysisResult.files}
            edges={analysisResult.edges}
            selectedFile={selectedFile}
            onSelectFile={setSelectedFile}
          />
        </div>
      )}
    </div>
  )
}

export default App
