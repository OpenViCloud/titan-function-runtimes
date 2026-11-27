// A module that blocks the event loop for 1.5 s while it is imported: SIGTERM arrives while it is loading.
const end = Date.now() + 1500;
while (Date.now() < end);

export const handler = () => 'slow';
