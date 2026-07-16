export function createSingleFlight<K, V>(
  operation: (key: K) => Promise<V>,
): (key: K) => Promise<V> {
  const inFlight = new Map<K, Promise<V>>();

  return (key: K): Promise<V> => {
    const existing = inFlight.get(key);
    if (existing !== undefined) return existing;

    const promise = operation(key).finally(() => {
      if (inFlight.get(key) === promise) inFlight.delete(key);
    });
    inFlight.set(key, promise);
    return promise;
  };
}
