export function ErrorEvent({ error }: { error: string }) {
  return (
    <div className="my-3 rounded-2xl border border-red-700/40 bg-red-900/20 px-4 py-3 text-[15px] text-red-300">
      {error}
    </div>
  );
}
