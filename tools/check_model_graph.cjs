const assert = require('node:assert/strict');
const { test } = require('node:test');
const G = require('../src/simlab/static/studio/model-graph.js');
const machine = name => ({ name, cycle_time_seconds: 60, availability: .9, mttr_seconds: 80, idle_power_kw: 5, processing_power_kw: 10 });
const buffer = name => ({ name, capacity: 5, delay_seconds: 10 });
const config = { name: 'fixture', machines: ['M1', 'M2', 'M3'].map(machine), buffers: ['B1', 'B2'].map(buffer),
  until_seconds: 7200, warmup_seconds: 60, replications: 3, base_seed: 7, confidence_level: .95, raw_buffer_capacity: 999,
  breaks: [{start_second: 60000, end_second: 62000}] };
test('existing serial model round-trips exactly and coordinates do not change config or paper mode', () => {
  const graph = G.fromConfig(config); graph.nodes[1].x = 1700; graph.nodes[1].y = 420;
  const checked = G.inspect(graph, config, 'paper', config);
  assert.equal(checked.ok, true); assert.equal(checked.mode, 'paper'); assert.deepEqual(checked.config, config);
  graph.nodes[1].data.cycle_time_seconds = 80;
  assert.equal(G.inspect(graph, config, 'paper', config).mode, 'paper');
});
test('drag-added independent device/container connected into line preserve parameters and compile in edge order', () => {
  const graph = G.fromConfig(config), added = G.add(graph, 'machine', 25, 25), container = G.add(graph, 'buffer', 180, 25);
  graph.nodes.find(item => item.id === added).data.name = 'New machine';
  const tail = graph.edges.find(edge => edge.to === 'sink').from;
  graph.edges = graph.edges.filter(edge => edge.to !== 'sink');
  G.connect(graph, tail, container); G.connect(graph, container, added); G.connect(graph, added, 'sink');
  const result = G.inspect(graph, config, 'paper', config);
  assert.equal(result.ok, true); assert.equal(result.mode, 'custom');
  assert.deepEqual(result.config.machines.slice(0, 3), config.machines);
  assert.equal(result.config.machines.at(-1).name, 'New machine');
  for (const key of ['until_seconds', 'warmup_seconds', 'replications', 'base_seed', 'confidence_level', 'raw_buffer_capacity', 'breaks']) assert.deepEqual(result.config[key], config[key]);
  const reordered = [graph.nodes[0].id, added, container, 'n1', 'n2', 'n3', 'n4', 'n5', 'sink'];
  graph.edges = []; for (let i = 1; i < reordered.length; i++) G.connect(graph, reordered[i - 1], reordered[i]);
  assert.deepEqual(G.inspect(graph, config, 'custom', config).config.machines.map(item => item.name), ['New machine', 'M1', 'M2', 'M3']);
});
test('ports reject duplicate links, branches, self-links, invalid node types and cycles', () => {
  const graph = G.fromConfig(config), before = structuredClone(graph);
  assert.throws(() => G.connect(graph, 'source', 'n1'), /已存在/);
  assert.throws(() => G.connect(graph, 'source', 'n3'), /已有连接/);
  assert.throws(() => G.connect(graph, 'n1', 'n1'), /自身/);
  assert.throws(() => G.connect(graph, 'n1', 'n3'), /连接顺序/);
  assert.deepEqual(graph, before);
  graph.edges = graph.edges.filter(edge => edge.from !== 'source' && edge.to !== 'sink');
  const b = G.add(graph, 'buffer', 20, 20); G.connect(graph, 'n5', b);
  assert.throws(() => G.connect(graph, b, 'n1'), /环路/);
});
test('all disconnected components, tampered cycles/branches and missing links block application', () => {
  const graph = G.fromConfig(config); graph.edges = graph.edges.filter(edge => edge.to !== 'n3');
  assert.equal(G.inspect(graph, config, 'custom', config).ok, false);
  graph.edges.push({from:'n5', to:'n2'});
  assert.equal(G.inspect(graph, config, 'custom', config).ok, false);
  const separate = G.fromConfig(config); G.add(separate, 'machine', 0, 0); G.add(separate, 'buffer', 0, 0);
  assert.equal(G.inspect(separate, config, 'custom', config).ok, false);
});
test('names and numeric/physical constraints remain enforced with useful object identifiers', () => {
  for (const [id, key, value] of [['n1','name','M2'], ['n1','name',' '], ['n1','availability',0], ['n1','mttr_seconds',0],
    ['n1','cycle_time_seconds',''], ['n1','idle_power_kw',Infinity], ['n1','processing_power_kw',-1], ['n2','capacity',1.2], ['n2','delay_seconds',-1]]) {
    const graph = G.fromConfig(config); graph.nodes.find(item => item.id === id).data[key] = value;
    const result = G.inspect(graph, config, 'custom', config); assert.equal(result.ok, false, key);
    assert.ok(result.issues.some(issue => issue.nodeId === id), key);
  }
  const graph = G.fromConfig(config); graph.nodes.find(item => item.id === 'n2').data.capacity = 8;
  assert.equal(G.inspect(graph, config, 'paper', config).mode, 'custom');
});
test('delete removes incident edges; terminals stay; maximums and single-device models work', () => {
  const graph = G.fromConfig(config); G.remove(graph, 'n3');
  assert.ok(graph.edges.every(edge => edge.from !== 'n3' && edge.to !== 'n3'));
  assert.throws(() => G.remove(graph, 'source'), /保留/);
  const single = {...config, machines:[machine('single')], buffers:[]};
  assert.deepEqual(G.inspect(G.fromConfig(single), single, 'custom', config).config, single);
  const maximum = G.fromConfig(single);
  for (let i = 0; i < 11; i++) { G.add(maximum, 'machine', 0, 0); G.add(maximum, 'buffer', 0, 0); }
  assert.throws(() => G.add(maximum, 'machine', 0, 0), /12/);
  assert.throws(() => G.add(maximum, 'buffer', 0, 0), /11/);
});
