const mk = (t) => { const s = { __type: t, default: (d) => ({ ...s, __default: d }), min: () => s }; return s; };
const z = { object: (shape) => ({ __type: 'object', shape }), string: () => mk('string'), boolean: () => mk('boolean'), number: () => mk('number') };
export default z;
