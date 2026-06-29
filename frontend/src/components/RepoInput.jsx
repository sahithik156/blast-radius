/*
RepoInput.jsx

PURPOSE OF THIS FILE:
The text box + button at the top of the app where the user pastes a GitHub
URL and kicks off analysis. This component is "dumb" on purpose: it only
manages the text typed into the box, it doesn't know HOW analysis works or
what happens with the result, it just calls the onAnalyze function that
App.jsx gave it. This separation (input component just collects input,
parent decides what to do with it) keeps this component reusable and easy
to reason about in isolation.
*/

import { useState } from 'react'

function RepoInput({ onAnalyze, isLoading }) {
  const [url, setUrl] = useState('')

  function handleSubmit(e) {
    // WHY e.preventDefault(): without this, submitting the form (e.g. by
    // pressing Enter in the text box) would trigger a full browser page
    // reload, which is the default HTML form behavior. We want this to be
    // a single-page app interaction (just run the fetch, don't reload),
    // so we explicitly stop that default behavior.
    e.preventDefault()

    // WHY .trim(): guards against submitting a URL that's just whitespace
    // (e.g. the user pasted a URL with a trailing space, or hit submit on
    // an empty-but-not-truly-empty box), which would otherwise pass our
    // empty check below but still send empty content to the backend.
    const trimmedUrl = url.trim()
    if (trimmedUrl) {
      onAnalyze(trimmedUrl)
    }
  }

  return (
    <form className="repo-input" onSubmit={handleSubmit}>
      <input
        type="text"
        placeholder="https://github.com/owner/repo"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        disabled={isLoading}
      />
      <button type="submit" disabled={isLoading || !url.trim()}>
        {isLoading ? 'Analyzing...' : 'Analyze'}
      </button>
    </form>
  )
}

export default RepoInput
