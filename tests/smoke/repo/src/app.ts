import { client } from "./ff";
export function checkout() {
  if (client.boolVariation("smoke-flag", ctx, false)) {
    return newFlow();
  } else {
    return oldFlow();
  }
}
