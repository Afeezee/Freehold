export function Mark({ size = 24 }: { size?: number }) {
  // Eight blocks around a centre, with one deliberately absent: the chunks
  // a file is split into, the key that reads them, and the gap that means
  // you can take your data elsewhere.
  const cells = [
    [0, 0], [9, 0], [18, 0],
    [0, 9],
    [0, 18], [9, 18], [18, 18],
  ];
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true"
         focusable="false">
      {cells.map(([x, y]) => (
        <rect key={`${x}-${y}`} x={x} y={y} width="6" height="6" rx="1.4"
              fill="currentColor" />
      ))}
      <rect x="9" y="9" width="6" height="6" rx="1.4" fill="var(--accent)" />
    </svg>
  );
}
