// Output formatting helpers for the mini fixture app.

function formatOutput(x) {
  return wrap(sanitize(x));
}

function sanitize(x) {
  return x.trim();
}

function wrap(x) {
  return "[" + x + "]";
}

module.exports = { formatOutput, sanitize, wrap };
