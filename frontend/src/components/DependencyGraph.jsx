/*
DependencyGraph.jsx

PURPOSE OF THIS FILE:
Renders the interactive node-link diagram of the repo's file dependencies,
this is the graph-explorer view from our earlier mockup. Uses
react-force-graph-2d, which handles the actual physics simulation (nodes
repelling each other, edges pulling connected nodes together) so we don't
have to write graph-layout math ourselves, that's a genuinely hard problem
on its own and not the part of this project we want to spend time on.

WHY WE COMPUTE BLAST RADIUS HIGHLIGHTING HERE, NOT JUST PASS A FLAT NODE LIST:
The mockup's whole point was: selecting a node visually distinguishes
"selected" / "direct dependent" / "indirect dependent" / "unrelated". The
API only gives us a flat edge list (the same data both this graph view and
the risk list use), it's THIS component's job to walk that edge list and
figure out which category each node falls into relative to whatever is
currently selected, then color nodes accordingly.
*/

import { useMemo, useRef, useEffect } from 'react'
import ForceGraph2D from 'react-force-graph-2d'

function buildGraphData(files, edges, selectedFile) {
  // WHY WE BUILD A LOOKUP OF "WHO POINTS AT WHOM" FIRST (predecessors):
  // Our edges mean "from depends on to" (e.g. {from: "app.py", to:
  // "ctx.py"} means app.py imports ctx.py). To find "what's affected if
  // ctx.py changes," we need everything that has an edge POINTING AT
  // ctx.py, i.e. everywhere "to" matches ctx.py. Precomputing this
  // direction as a lookup table (rather than re-scanning the whole edge
  // list for every node) keeps the blast-radius calculation fast even on
  // a graph with hundreds of edges.
  const predecessors = {}
  edges.forEach(({ from, to }) => {
    if (!predecessors[to]) predecessors[to] = []
    predecessors[to].push(from)
  })

  // Direct dependents: anything with a one-hop edge pointing at the
  // selected file.
  const directDependents = new Set(selectedFile ? predecessors[selectedFile] || [] : [])

  // Indirect dependents: walk the predecessor chain outward (breadth-first)
  // from the direct dependents, collecting everything reachable. This
  // mirrors what nx.ancestors() does on the backend, just reimplemented
  // here in JS since we only have the flat edge list on the frontend, not
  // the original networkx graph object.
  const indirectDependents = new Set()
  if (selectedFile) {
    const queue = [...directDependents]
    const visited = new Set([selectedFile, ...directDependents])

    while (queue.length > 0) {
      const current = queue.shift()
      const preds = predecessors[current] || []
      for (const p of preds) {
        if (!visited.has(p)) {
          visited.add(p)
          indirectDependents.add(p)
          queue.push(p)
        }
      }
    }
  }

  const nodes = files.map((file) => {
    let category = 'unrelated'
    if (file.filepath === selectedFile) category = 'selected'
    else if (directDependents.has(file.filepath)) category = 'direct'
    else if (indirectDependents.has(file.filepath)) category = 'indirect'

    return {
      id: file.filepath,
      riskScore: file.risk_score,
      category,
    }
  })

  const links = edges.map((e) => ({ source: e.from, target: e.to }))

  return { nodes, links }
}

// WHY THESE SPECIFIC COLORS: matching the mockup's palette (coral for
// selected, amber for direct impact, gray for indirect/unrelated), so the
// real app visually matches what we already validated with the user.
const CATEGORY_COLORS = {
  selected: '#D85A30',
  direct: '#E8A23D',
  indirect: '#888780',
  unrelated: '#C9C8C2',
}

function DependencyGraph({ files, edges, selectedFile, onSelectFile }) {
  const graphRef = useRef()

  // WHY useMemo HERE: buildGraphData does real computational work (the
  // breadth-first walk above). Without useMemo, this would re-run on
  // EVERY render, including renders triggered by unrelated state changes.
  // Memoizing means it only recalculates when files, edges, or
  // selectedFile actually change.
  const graphData = useMemo(
    () => buildGraphData(files, edges, selectedFile),
    [files, edges, selectedFile]
  )

  useEffect(() => {
    // WHY WE CALL zoomToFit: react-force-graph starts every new graph
    // zoomed to a default level that often doesn't fit the actual node
    // layout, leaving the user looking at an awkwardly cropped view.
    // Calling zoomToFit shortly after the graph data changes (e.g. a new
    // repo was just analyzed) auto-frames the whole graph nicely.
    if (graphRef.current) {
      setTimeout(() => graphRef.current.zoomToFit(400, 50), 300)
    }
  }, [files, edges])

  return (
    <div className="dependency-graph">
      <ForceGraph2D
        ref={graphRef}
        graphData={graphData}
        nodeLabel="id"
        nodeColor={(node) => CATEGORY_COLORS[node.category]}
        nodeVal={(node) => 2 + node.riskScore / 20}
        linkColor={() => 'rgba(150, 150, 150, 0.3)'}
        linkDirectionalArrowLength={4}
        linkDirectionalArrowRelPos={1}
        onNodeClick={(node) => onSelectFile(node.id === selectedFile ? null : node.id)}
      />
    </div>
  )
}

export default DependencyGraph
